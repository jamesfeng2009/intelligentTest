"""总控 Agent（Orchestrator）。

职责边界（对齐项目计划 §3.1）：
✅ 接收测试需求，解析测试目标
✅ 拆解任务：UI 测试 / API 测试 / 白盒测试
✅ 调度子 Agent，按状态机流转
✅ 管理审批节点和中断点
✅ 汇总结果，生成最终报告
❌ 不亲自写测试代码 / 不亲自执行测试

流程：IDLE → ANALYZE → [计划审批] → SETUP → 子Agent并行 → VERIFY → [报告确认] → REPORT → DONE
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import get_settings
from core.llm import BaseLLM, create_llm
from core.logging_setup import get_logger, log_event, set_task_context, set_agent_context
from core.models import TestPlan, TestTask
from harness.approvals import ApprovalManager
from harness.artifacts import ArtifactManager
from harness.state_machine import IllegalTransitionError, State, StateMachine

logger = get_logger("orchestrator")


class Orchestrator:
    def __init__(self, llm: BaseLLM | None = None, auto_approve: bool = True) -> None:
        self.llm = llm or create_llm()
        self.auto_approve = auto_approve
        self.sm = StateMachine()
        self.artifacts: ArtifactManager | None = None
        self.approvals: ApprovalManager | None = None
        self.task: TestTask | None = None

    # ---------------- 主入口 ----------------
    def run(self, task: TestTask) -> dict:
        set_task_context(task.task_id)
        set_agent_context("orchestrator")
        self.task = task
        self.artifacts = ArtifactManager(task.task_id)
        self.approvals = ApprovalManager(auto_approve=self.auto_approve)
        task.artifacts_dir = str(self.artifacts.run_dir)
        log_event(logger, "task_start", {"task_id": task.task_id, "requirement": task.requirement[:120]})

        self._transition(State.ANALYZE, "接收测试需求")

        # 1) 需求解析 → 测试计划
        plan = self._analyze()
        self.artifacts.write("orchestrator", "test_plan", plan.to_dict(), "json")
        self.artifacts.write("orchestrator", "需求解析", self._plan_markdown(plan), "md")

        # 2) 中断点1：计划审批
        self._transition(State.WAIT_APPROVAL, "生成测试计划，等待审批")
        req = self.approvals.request("计划审批", {"plan": plan.to_dict()})
        if req.decision and req.decision.value == "rejected":
            self.artifacts.write("orchestrator", "审批结果", req.to_dict(), "json")
            self.sm.transition(State.FAILED, "计划审批被驳回")
            return self._finalize(status="rejected", reason="计划审批被驳回")

        # 3) 执行阶段
        self._transition(State.SETUP, "计划审批通过，准备执行")
        results = self._dispatch(plan)

        # 4) 验证阶段
        self._transition(State.VERIFY, "子 Agent 执行完成，进入验证")
        verified = self._verify(results)

        # 5) 中断点4：报告确认
        self._transition(State.REPORT, "生成最终报告")
        report = self._build_report(plan, results, verified)
        self.artifacts.write("orchestrator", "最终报告", report, "md")
        self.approvals.request("报告确认", {"report_summary": report[:200]})

        self._transition(State.DONE, "任务完成")
        return self._finalize(status="done", report=report, results=results)

    # ---------------- 内部步骤 ----------------
    def _analyze(self) -> TestPlan:
        assert self.task is not None
        system = (Path(__file__).parent.parent / "prompts" / "orchestrator.md").read_text(encoding="utf-8")
        raw = self.llm.chat_json(system, self.task.requirement)
        # schema 校验兜底：scope 必须是合法集合
        scope = [s for s in raw.get("scope", ["api"]) if s in ("ui", "api", "whitebox")]
        if not scope:
            scope = ["api"]
        plan = TestPlan(
            scope=scope,
            strategy=raw.get("strategy", ""),
            risk_points=raw.get("risk_points", []),
            scenarios=raw.get("scenarios", []),
            estimates=raw.get("estimates", {}),
            summary=raw.get("summary", ""),
        )
        self.task.test_scope = plan.to_dict()
        log_event(logger, "plan_ready", {"scope": plan.scope, "scenarios": len(plan.scenarios)})
        return plan

    def _dispatch(self, plan: TestPlan) -> dict[str, Any]:
        """按 scope 调度子 Agent。各子 Agent 返回 {summary, results, artifacts} 结构。"""
        assert self.task is not None
        outputs: dict[str, Any] = {}
        # 导入子 Agent（延迟，避免循环依赖）
        from .api_tester import APITester
        from .ui_tester import UITester
        from .whitebox_tester import WhiteboxTester

        scope = list(plan.scope)
        # 白盒需要仓库信息
        if "whitebox" in scope and not self.task.repo_path:
            logger.warning("白盒测试缺少 repo_path，跳过白盒")
            scope.remove("whitebox")

        for kind in scope:
            try:
                if kind == "api":
                    self.sm.transition(State.API_TEST, "调度 API 测试 Agent")
                    outputs["api"] = APITester(self.llm, self.artifacts).run(self.task, plan)
                elif kind == "ui":
                    self.sm.transition(State.UI_TEST, "调度 UI 测试 Agent")
                    outputs["ui"] = UITester(self.llm, self.artifacts).run(self.task, plan)
                elif kind == "whitebox":
                    self.sm.transition(State.WHITEBOX_TEST, "调度白盒测试 Agent")
                    outputs["whitebox"] = WhiteboxTester(self.llm, self.artifacts).run(self.task, plan)
            except Exception as e:  # noqa: BLE001
                logger.exception("子 Agent %s 执行失败", kind)
                outputs[kind] = {"summary": f"执行失败: {e}", "results": [], "error": str(e)}
        return outputs

    def _verify(self, outputs: dict[str, Any]) -> dict[str, Any]:
        """调用 Verifier 对失败用例分类。"""
        from .verifier import Verifier

        verifier = Verifier(self.llm, self.artifacts)
        verified: dict[str, Any] = {}
        for kind, out in outputs.items():
            results = out.get("results", [])
            failed = [r for r in results if r.get("status") == "failed"]
            if failed:
                classified = verifier.classify(kind, failed)
                # 把分类写回结果
                by_name = {c["name"]: c for c in classified}
                for r in results:
                    if r.get("status") == "failed" and r.get("name") in by_name:
                        r["category"] = by_name[r["name"]]["category"]
            verified[kind] = {"total": len(results), "failed": len(failed), "classified": len(classified) if failed else 0}
        return verified

    def _build_report(self, plan: TestPlan, results: dict[str, Any], verified: dict[str, Any]) -> str:
        lines = [
            f"# 测试报告 · {self.task.task_id}",
            "",
            f"- 需求：{self.task.requirement[:200]}",
            f"- 范围：{', '.join(plan.scope)}",
            f"- 策略：{plan.strategy or '-'}",
            "",
            "## 执行结果",
        ]
        for kind, out in results.items():
            results_list = out.get("results", [])
            passed = sum(1 for r in results_list if r.get("status") == "passed")
            failed = sum(1 for r in results_list if r.get("status") == "failed")
            total = len(results_list)
            rate = f"{passed / total * 100:.1f}%" if total else "-"
            lines.append(f"- **{kind}**：{passed}/{total} 通过（{rate}），失败 {failed}")
            for r in results_list:
                if r.get("status") == "failed":
                    lines.append(f"  - ❌ {r.get('name')}：{r.get('error', '')[:100]} [分类: {r.get('category', '待确认')}]")
        lines.append("")
        lines.append("## 验证汇总")
        for kind, v in verified.items():
            lines.append(f"- {kind}: {v}")
        lines.append("")
        lines.append("> 报告由 AI 测试智能体自动生成，失败分类结果建议人工复核。")
        return "\n".join(lines)

    def _plan_markdown(self, plan: TestPlan) -> str:
        return f"""# 测试计划
- 范围：{', '.join(plan.scope)}
- 策略：{plan.strategy}
- 风险点：{', '.join(plan.risk_points)}
- 场景：{len(plan.scenarios)} 个
"""

    def _transition(self, target: State, reason: str) -> None:
        try:
            self.sm.transition(target, reason)
        except IllegalTransitionError as e:
            logger.warning("状态机拒绝跳步: %s", e)

    def _finalize(self, status: str, reason: str = "", report: str = "", results: dict | None = None) -> dict:
        assert self.artifacts is not None and self.task is not None
        out = {
            "task_id": self.task.task_id,
            "status": status,
            "reason": reason,
            "state": self.sm.state.value,
            "report": report,
            "results": results or {},
            "artifacts": self.artifacts.collect_artifacts(),
            "approvals": self.approvals.all() if self.approvals else [],
            "trace": self.sm.history(),
        }
        self.artifacts.write("orchestrator", "task_result", out, "json")
        log_event(logger, "task_done", {"task_id": self.task.task_id, "status": status, "state": self.sm.state.value})
        return out

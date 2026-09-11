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
        self._current_plan = plan
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
        # P1：需求三级解析（L1 结构 + L2 条目 + L3 要素），结构化条目注入测试计划生成
        # P2：相似需求/历史缺陷 RAG 上下文注入（知识库可用时）
        from knowledge.requirement_parser import parse_requirement, render_requirement_md
        from knowledge.requirement_rag import build_requirement_context, project_id_from

        extra_context = ""
        pid = project_id_from(self.task)
        if pid:
            try:
                from web.db import SessionLocal

                with SessionLocal() as db:
                    extra_context = build_requirement_context(db, pid, self.task.requirement)
            except Exception as e:  # noqa: BLE001 知识库不可用不阻断
                log_event(logger, "rag_context_skip", {"reason": str(e)})

        req_result = parse_requirement(self.task.requirement, self.llm,
                                       endpoints=self._load_endpoints(),
                                       extra_context=extra_context)
        self._req_result = req_result
        self.artifacts.write("orchestrator", "需求解析", req_result.to_dict(), "json")
        self.artifacts.write("orchestrator", "需求解析", render_requirement_md(req_result), "md")

        item_summary = "\n".join(
            f"- {it.id} {it.title}（{it.priority}）：{'；'.join(it.acceptance[:2]) or it.desc[:40]}"
            for it in req_result.items
        )
        user = f"{self.task.requirement}\n\n【已解析需求条目】\n{item_summary or '（未能结构化解析，按原文处理）'}"
        raw = self.llm.chat_json(system, user)
        # schema 校验兜底：scope 必须是合法集合
        scope = [s for s in raw.get("scope", ["api"]) if s in ("ui", "api", "whitebox", "functional")]
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
        """按 scope 调度子 Agent（P3：注册表查表路由 + enabled 过滤 + 前置条件）。

        路由链：build_registry() 注册表 → agent_configs enabled/model/harness 覆盖
        → spec.requires 前置条件 → 状态机节点 → entry(llm, artifacts).run()。
        functional 主线内部再调 functional_reviewer（B 模型评审，横切质量门）。
        """
        assert self.task is not None
        from .registry import build_registry, check_requires, load_agent_configs

        registry = build_registry()
        cfgs = load_agent_configs()
        # 主线顺序 = 注册顺序（mainline=True；编排者/横切 Gate 不参与）
        priority = [s.name for s in registry.values() if s.mainline]
        scope = [k for k in priority if k in plan.scope]

        outputs: dict[str, Any] = {}
        for kind in scope:
            spec = registry[kind]
            cfg = cfgs.get(kind) or {}
            if cfg.get("enabled") is False:
                log_event(logger, "agent_skipped", {"agent": kind, "reason": "disabled（agent_configs）"})
                continue
            missing = [r for r in spec.requires if not check_requires(self.task, r)]
            if missing:
                log_event(logger, "agent_skipped", {"agent": kind, "reason": f"缺少前置条件 {missing}"})
                continue
            try:
                self.sm.transition(spec.state, f"调度 {spec.name} Agent")
                if kind == "functional":
                    outputs[kind] = self._run_functional(registry, cfgs)
                else:
                    runner = spec.entry(self.llm, self.artifacts)
                    outputs[kind] = runner.run(self.task, plan)
            except Exception as e:  # noqa: BLE001
                logger.exception("子 Agent %s 执行失败", kind)
                outputs[kind] = {"summary": f"执行失败: {e}", "results": [], "error": str(e)}
        return outputs

    def _run_functional(self, registry: dict, cfgs: dict) -> dict[str, Any]:
        """功能测试闭环（P3：生成与评审角色均经注册表路由）：
        A 模型生成 → B 模型评审（可启停）→ 追溯矩阵。"""
        assert self.task is not None
        gen = registry["functional"].entry(self.llm, self.artifacts).run(self.task, self._plan)
        if not gen.get("cases"):
            return {"summary": gen.get("summary", "功能用例生成失败"), "cases": [], "results": []}

        # B 模型独立评审（横切质量门）：agent_configs 可停用
        review_cfg = cfgs.get("functional_reviewer") or {}
        if review_cfg.get("enabled") is False:
            log_event(logger, "review_skipped", {"reason": "functional_reviewer 已停用"})
            self.sm.transition(State.CASE_REVIEW, "评审 Agent 已停用（跳过 B 模型评审）")
            rev = {"overall": "skipped", "summary": "评审 Agent 已停用，未执行 B 模型评审",
                   "findings": [], "missing_scenarios": []}
        else:
            from core.llm import create_llm
            from .functional_reviewer import FunctionalReviewer

            self.sm.transition(State.CASE_REVIEW, "B 模型独立评审功能用例")
            review_llm = create_llm(role="review")   # B 模型；未配置时回退主模型/mock
            reviewer = FunctionalReviewer(review_llm, self.artifacts, generator_llm=self.llm)
            endpoints = self._load_endpoints()
            rev = reviewer.run(self.task, self._plan, gen["cases"], endpoints)

        # P1b：需求-用例追溯矩阵（需求条目 ↔ 功能用例覆盖缺口）
        traceability: list[dict] = []
        req_result = getattr(self, "_req_result", None)
        if req_result is not None and req_result.items:
            from knowledge.requirement_parser import build_traceability, render_traceability_md

            traceability = build_traceability(req_result.items, gen["cases"])
            self.artifacts.write("functional_reviewer", "追溯矩阵", traceability, "json")
            self.artifacts.write("functional_reviewer", "追溯矩阵", render_traceability_md(traceability), "md")

        return {
            "summary": f"{gen['summary']}；{rev['summary']}",
            "results": [],
            "cases": gen["cases"],
            "review": rev,
            "traceability": traceability,
        }

    def _load_endpoints(self) -> list[dict]:
        """加载接口文档端点（供评审程序硬检查）。"""
        from .api_tester import parse_openapi

        assert self.task is not None
        spec_path = (self.task.meta.get("openapi_path")
                     or str(Path(__file__).parent.parent / "examples" / "openapi_demo.yaml"))
        try:
            return parse_openapi(spec_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("接口文档解析失败，程序硬检查降级: %s", e)
            return []

    @property
    def _plan(self) -> TestPlan:
        """当前测试计划（_dispatch 调用链中由 run 设置到实例）。"""
        return self._current_plan

    def _verify(self, outputs: dict[str, Any]) -> dict[str, Any]:
        """调用 Verifier 对失败用例分类。"""
        from .verifier import Verifier

        verifier = Verifier(self.llm, self.artifacts)
        verified: dict[str, Any] = {}
        for kind, out in outputs.items():
            # 功能测试：非可执行用例，走 B 模型评审而非失败分类
            if kind == "functional":
                review = out.get("review") or {}
                verified[kind] = {"cases": len(out.get("cases", [])),
                                  "review": review.get("overall", "-"),
                                  "findings": len(review.get("findings", []))}
                continue
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
            # 功能测试：附评审结论
            if kind == "functional":
                review = out.get("review") or {}
                lines.append(f"  - 功能用例：{len(out.get('cases', []))} 条；B 模型评审："
                             f"**{review.get('overall', '-')}**（{review.get('summary', '')}）")
                for f in review.get("findings", [])[:5]:
                    lines.append(f"    - [{f.get('verdict')}/{f.get('severity')}] {f.get('case_id', '?')} {f.get('issue', '')[:80]}")
                if review.get("revised_cases"):
                    lines.append(f"  - 修订版用例：{len(review['revised_cases'])} 条（见 functional_reviewer 产物）")
                gaps = [r for r in out.get("traceability", []) if r.get("status") == "gap"]
                lines.append(f"  - 需求-用例追溯：{len(out.get('traceability', []))} 个需求条目，"
                             f"**{len(gaps)} 个未覆盖缺口**" + (f"：{', '.join(g['item_title'] for g in gaps[:4])}" if gaps else ""))
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

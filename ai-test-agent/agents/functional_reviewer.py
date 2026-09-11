"""功能用例评审 Agent（FunctionalReviewer）—— 评审侧（B 模型）。

流程：
1. 程序硬检查（不依赖模型）：接口引用必须存在于接口文档；必备字段非空
2. B 模型独立评审：逐条核对用例是否符合【需求】与【接口文档】描述的业务逻辑
3. 合并程序 findings 与 LLM findings（去重）
4. 修订回环：存在 fail 时，把评审意见回灌 A 模型生成修订版用例
5. 落盘 functional_review.json / functional_review.md

设计原则：LLM 只做语义判断；可量化的检查（引用合法性/字段完整性）由程序完成，
确保"接口文档一致性"这一维度不依赖模型能力。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import TestPlan, TestTask
from harness.artifacts import ArtifactManager

logger = get_logger("functional_reviewer")

# 程序硬检查不依赖 LLM，永远生效（无论 mock / 真实模型）
RULE_FINDING_SOURCE = "程序硬检查"


class FunctionalReviewer:
    def __init__(self, review_llm: BaseLLM, artifacts: ArtifactManager | None = None,
                 generator_llm: BaseLLM | None = None) -> None:
        """review_llm = B 模型（评审）；generator_llm = A 模型（修订回环）。"""
        self.review_llm = review_llm
        self.generator_llm = generator_llm or review_llm
        self.artifacts = artifacts

    def run(self, task: TestTask, plan: TestPlan, cases: list[dict], endpoints: list[dict]) -> dict:
        set_agent_context("functional_reviewer")
        endpoint_map = {(e["method"].upper(), e["path"]): e for e in endpoints}

        # 1) 程序硬检查
        rule_findings = self._rule_check(cases, endpoint_map)

        # 2) B 模型独立评审
        llm_result = self._llm_review(task.requirement, endpoints, cases)

        # 3) 合并（程序 findings 恒保留，LLM findings 按 case_id+issue 去重）
        findings = self._merge_findings(rule_findings, llm_result.get("findings", []))
        missing = list(llm_result.get("missing_scenarios", []) or [])
        overall = "issues_found" if any(f["verdict"] != "pass" for f in findings) else llm_result.get("overall", "pass")

        # 4) 修订回环：存在 fail 用例时，评审意见回灌 A 模型
        revised: list[dict] = []
        if any(f["verdict"] == "fail" for f in findings):
            revised = self._revise(task.requirement, endpoints, cases, findings, missing)
            log_event(logger, "revised_cases", {"count": len(revised)})

        review = {
            "overall": overall,
            "summary": self._summary(overall, len(cases), len(findings), len(revised)),
            "findings": findings,
            "missing_scenarios": missing,
            "revised_cases": revised,
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            "review_model": getattr(self.review_llm, "name", "unknown"),
            "generator_model": getattr(self.generator_llm, "name", "unknown"),
        }
        self._write_artifacts(task, review)
        stats = {"total_cases": len(cases), "findings": len(findings),
                 "fails": sum(1 for f in findings if f["verdict"] == "fail"),
                 "revised": len(revised)}
        log_event(logger, "functional_review_done", stats)
        return {"summary": review["summary"], "overall": overall, "findings": findings,
                "missing_scenarios": missing, "revised_cases": revised, "stats": stats}

    # ---------------- 程序硬检查 ----------------
    @staticmethod
    def _rule_check(cases: list[dict], endpoint_map: dict) -> list[dict]:
        findings: list[dict] = []
        for c in cases:
            cid = str(c.get("id", "?"))
            for ep in c.get("involved_endpoints", []) or []:
                m = str(ep.get("method", "")).upper()
                p = str(ep.get("path", ""))
                if (m, p) not in endpoint_map:
                    findings.append({
                        "case_id": cid, "verdict": "fail", "severity": "blocker",
                        "issue": f"引用了接口文档中不存在的端点 {m} {p}，与开发接口文档不一致",
                        "suggestion": "改为接口清单中的真实端点，或删除该引用", "source": RULE_FINDING_SOURCE,
                    })
            if not str(c.get("title", "")).strip():
                findings.append({
                    "case_id": cid, "verdict": "fail", "severity": "high",
                    "issue": "用例缺少标题，无法定位业务场景",
                    "suggestion": "补充业务场景标题", "source": RULE_FINDING_SOURCE,
                })
            if not str(c.get("expected", "")).strip():
                findings.append({
                    "case_id": cid, "verdict": "fail", "severity": "high",
                    "issue": "用例缺少预期结果，无法判定是否符合需求",
                    "suggestion": "写明状态码/关键字段/业务状态", "source": RULE_FINDING_SOURCE,
                })
        return findings

    # ---------------- B 模型评审 ----------------
    def _llm_review(self, requirement: str, endpoints: list[dict], cases: list[dict]) -> dict:
        system = (Path(__file__).parent.parent / "prompts" / "functional_reviewer.md").read_text(encoding="utf-8")
        user = (
            f"functional_review 任务：请独立评审以下功能测试用例是否符合需求与接口文档逻辑\n"
            f"测试需求：{requirement}\n"
            f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}\n"
            f"待评审用例：{json.dumps(cases, ensure_ascii=False)}"
        )
        try:
            raw = self.review_llm.chat_json(system, user)
            return {
                "overall": str(raw.get("overall", "pass")),
                "summary": str(raw.get("summary", "")),
                "findings": [f for f in raw.get("findings", []) if isinstance(f, dict)],
                "missing_scenarios": [str(s) for s in (raw.get("missing_scenarios", []) or [])],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("B 模型评审失败，仅保留程序硬检查结果: %s", e)
            return {"overall": "issues_found", "summary": f"B 模型评审失败：{e}", "findings": [], "missing_scenarios": []}

    @staticmethod
    def _merge_findings(rule_findings: list[dict], llm_findings: list[dict]) -> list[dict]:
        merged = list(rule_findings)
        seen = {(f.get("case_id"), f.get("issue", "")[:40]) for f in merged}
        for f in llm_findings:
            if not f.get("issue"):
                continue
            key = (f.get("case_id"), str(f.get("issue", ""))[:40])
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "case_id": f.get("case_id", "?"),
                "verdict": f.get("verdict", "gap") if f.get("verdict") in ("pass", "fail", "gap") else "gap",
                "severity": f.get("severity", "medium") if f.get("severity") in ("blocker", "high", "medium", "low") else "medium",
                "issue": str(f.get("issue", "")),
                "suggestion": str(f.get("suggestion", "")),
                "source": "B 模型评审",
            })
        return merged

    # ---------------- 修订回环（A 模型） ----------------
    def _revise(self, requirement: str, endpoints: list[dict], cases: list[dict],
                findings: list[dict], missing: list[str]) -> list[dict]:
        """把评审意见回灌 A 模型，生成修订版用例；程序校验后返回。"""
        from agents.functional_tester import FunctionalTester
        from core.models import FUNCTIONAL_CATEGORIES

        system = (Path(__file__).parent.parent / "prompts" / "functional_tester.md").read_text(encoding="utf-8")
        user = (
            f"functional_case 任务：请根据评审意见修订以下功能测试用例\n"
            f"测试需求：{requirement}\n"
            f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}\n"
            f"评审意见：{json.dumps({'findings': findings, 'missing_scenarios': missing}, ensure_ascii=False)}\n"
            f"原用例：{json.dumps(cases, ensure_ascii=False)}\n"
            f"要求：修复评审中指出的问题（引用真实端点、补充预期结果、补齐缺失场景），保留合法用例，输出完整修订版。"
        )
        try:
            raw = self.generator_llm.chat_json(system, user)
            raw_cases = raw.get("cases", []) if isinstance(raw, dict) else raw
        except Exception as e:  # noqa: BLE001
            logger.warning("修订生成失败，保留原用例: %s", e)
            return []
        endpoint_map = {(e["method"].upper(), e["path"]): True for e in endpoints}
        valid = set(FUNCTIONAL_CATEGORIES)
        out: list[dict] = []
        for c in raw_cases:
            if not isinstance(c, dict) or c.get("category") not in valid:
                continue
            eps = [{"method": str(e.get("method", "")).upper(), "path": e.get("path", "")}
                   for e in (c.get("involved_endpoints", []) or []) if isinstance(e, dict)
                   and (str(e.get("method", "")).upper(), e.get("path", "")) in endpoint_map]
            if not eps or not str(c.get("title", "")).strip() or not str(c.get("expected", "")).strip():
                continue
            c["involved_endpoints"] = eps
            c["id"] = str(c.get("id") or f"RV-{len(out) + 1:03d}")
            out.append(c)
        return out

    # ---------------- 产物 ----------------
    def _write_artifacts(self, task: TestTask, review: dict) -> None:
        if not self.artifacts:
            return
        self.artifacts.write("functional_reviewer", "functional_review", review, "json")
        self.artifacts.write("functional_reviewer", "functional_review", self._render_markdown(task, review), "md")

    @staticmethod
    def _render_markdown(task: TestTask, review: dict) -> str:
        lines = [
            "# 功能用例独立评审报告（B 模型）",
            "",
            f"- 需求：{task.requirement[:200]}",
            f"- 评审模型：{review.get('review_model', '-')}（生成模型：{review.get('generator_model', '-')}）",
            f"- 总体结论：**{'发现需修订问题' if review['overall'] == 'issues_found' else '通过'}**",
            f"- 评审摘要：{review.get('summary', '')}",
            "",
            "## 评审意见",
            "",
            "| 用例 ID | 结论 | 严重级 | 问题 | 建议 | 来源 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for f in review.get("findings", []):
            lines.append(f"| {f.get('case_id', '-')} | {f.get('verdict')} | {f.get('severity')} "
                         f"| {f.get('issue', '')} | {f.get('suggestion', '')} | {f.get('source', '-')} |")
        lines.append("")
        lines.append("## 未覆盖场景")
        for s in review.get("missing_scenarios", []) or []:
            lines.append(f"- {s}")
        revised = review.get("revised_cases", []) or []
        lines.append("")
        lines.append(f"## 修订版用例（{len(revised)} 条）")
        for c in revised:
            eps = "; ".join(f"{e['method']} {e['path']}" for e in c.get("involved_endpoints", []))
            lines.append(f"- **{c.get('id')}** [{c.get('category')}] {c.get('title')} → {c.get('expected')}（{eps}）")
        return "\n".join(lines)

    @staticmethod
    def _summary(overall: str, total: int, findings: int, revised: int) -> str:
        if overall == "pass":
            return f"评审通过：{total} 条用例均符合需求与接口文档逻辑，未发现不符合项"
        return f"评审发现 {findings} 条意见，{revised} 条修订版用例已生成，建议人工复核后执行"

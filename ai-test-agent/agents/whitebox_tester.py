"""白盒测试 Agent（WhiteboxTester）—— 质量左移核心。

流程：
Step1 Git Diff 增量识别（文件→行→函数，四层）
Step2 影响面分析（上游调用者/下游被调用/风险等级）
Step2.5 契约变化检测（A10：枚举/签名/DTO/接口路由）
Step3 AI 代码走查（五维）+ 动态代码模式检查（A12：Job/MQ/延迟消息专属清单）
Step4 单测生成 + 语法校验 + 执行 + 覆盖率
Step5 结果输出（结构化产物）
"""
from __future__ import annotations

from pathlib import Path

from adapters.factory import get_adapter
from adapters.project_detect import detect_project_type
from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import TestPlan, TestTask
from harness.artifacts import ArtifactManager
from whitebox.code_reviewer import review_changed_functions
from whitebox.contract_check import detect_contract_changes
from whitebox.git_diff import get_changed_functions
from whitebox.impact_analysis import build_impact_report
from whitebox.pattern_check import check_patterns
from whitebox.test_generator import generate_and_execute

logger = get_logger("whitebox")


class WhiteboxTester:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts

    def run(self, task: TestTask, plan: TestPlan) -> dict:
        set_agent_context("whitebox_tester")
        repo = task.repo_path
        base, target = task.base_commit, task.target_commit
        if not repo or not base or not target:
            return {"summary": "缺少仓库/commit 信息，跳过白盒", "results": [], "error": "需要 repo_path/base_commit/target_commit"}

        # Step1: 变更分析
        changed_funcs = get_changed_functions(repo, base, target)
        log_event(logger, "diff_analyzed", {"files": len({f.file_path for f in changed_funcs}), "functions": len(changed_funcs)})
        if self.artifacts:
            self.artifacts.write("whitebox_tester", "变更分析", self._changed_summary(changed_funcs), "md")

        if not changed_funcs:
            return {"summary": "未发现可分析的变更函数（或语言不支持）", "results": [], "changed_functions": []}

        # Step0.5: 前后端工程识别（A8）
        engineering = detect_project_type(repo)
        log_event(logger, "engineering_type", {"type": engineering["type"], "evidence": engineering["evidence"]})

        # Step2: 影响面 + 风险
        impact = build_impact_report(repo, changed_funcs)
        if self.artifacts:
            self.artifacts.write("whitebox_tester", "影响面分析", impact, "json")
        high_risk = [r for r in impact["risk"] if r["risk_level"] == "high"]
        log_event(logger, "impact_done", {"high_risk": len(high_risk)})

        # Step2.5: 契约变化检测（A10）
        contract = detect_contract_changes(repo, base, target, changed_funcs)
        contract_high = [c for c in contract if c["severity"] in ("严重", "高")]
        if self.artifacts:
            self.artifacts.write("whitebox_tester", "契约变化检测", contract, "json")
        log_event(logger, "contract_done", {"findings": len(contract), "high": len(contract_high)})

        # Step3: 动态代码模式检查（A12）+ AI 代码走查（五维）
        pattern_findings = check_patterns(changed_funcs)
        pattern_hits = [p for p in pattern_findings if "checklist" in p]  # 汇总意见即命中数
        llm_findings = review_changed_functions(self.llm, changed_funcs, requirement=task.requirement)
        findings = pattern_findings + contract + llm_findings
        if self.artifacts:
            self.artifacts.write("whitebox_tester", "代码走查", findings, "json")
            self.artifacts.write("whitebox_tester", "动态模式检查",
                                 {"hits": pattern_hits, "findings": [f for f in pattern_findings if "checklist" not in f]},
                                 "json")
        log_event(logger, "pattern_done", {"hits": len(pattern_hits), "checks": len(pattern_findings)})

        # Step4: 按语言分组生成并执行单测
        by_lang: dict[str, list] = {}
        for f in changed_funcs:
            adapter = get_adapter(f.file_path)
            if adapter:
                by_lang.setdefault(adapter.language, []).append(f)
        unit_reports: dict[str, dict] = {}
        for lang, funcs in by_lang.items():
            adapter = get_adapter(funcs[0].file_path)
            report = generate_and_execute(self.llm, funcs, adapter)
            unit_reports[lang] = {
                "language": lang,
                "generated": report.get("generated", 0),
                "passed": report.get("passed", 0),
                "failed": report.get("failed", 0),
                "coverage": report.get("coverage"),
                "error": report.get("error", ""),
                "raw": report.get("raw", ""),
            }
            if self.artifacts:
                self.artifacts.write("whitebox_tester", f"单测报告_{lang}", unit_reports[lang], "json")
        log_event(logger, "unit_done", {k: v["passed"] for k, v in unit_reports.items()})

        # Step5: 汇总
        summary = (
            f"白盒分析完成：工程类型 {engineering['type']}，变更函数 {len(changed_funcs)} 个，高风险 {len(high_risk)} 个，"
            f"契约变化 {len(contract)} 处（高 {len(contract_high)}），动态模式 {len(pattern_hits)} 个，"
            f"走查意见 {len(findings)} 条，单测 {sum(v.get('generated', 0) for v in unit_reports.values())} 个"
        )
        return {
            "summary": summary,
            "results": [{"name": "whitebox_analysis", "status": "passed", "category": "分析完成"}],
            "engineering_type": engineering,      # A8 前后端工程识别
            "changed_functions": [f.to_dict() for f in changed_funcs],
            "impact": impact,
            "contract_changes": contract,        # A10
            "pattern_hits": pattern_hits,        # A12
            "findings": findings,
            "unit_reports": unit_reports,
        }

    @staticmethod
    def _changed_summary(changed_funcs) -> str:
        files: dict[str, list] = {}
        for f in changed_funcs:
            files.setdefault(f.file_path, []).append(f.function_name)
        lines = ["# 变更分析", "", f"- 变更函数数：{len(changed_funcs)}", ""]
        for path, funcs in files.items():
            lines.append(f"## {path}")
            for fn in funcs:
                lines.append(f"- {fn}")
        return "\n".join(lines)

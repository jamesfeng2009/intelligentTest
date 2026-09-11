"""验证 Agent（Verifier）—— 失败分类 + 指标计算。

职责（对齐项目计划 §3.5）：
1. 分析执行结果，区分"脚本错误" vs "真实 Bug"
2. 分类失败原因（UI 定位不准 / 接口报错 / 断言错误 / 业务逻辑错误）
3. 计算质量指标（通过率、覆盖率、失败分布）
"""
from __future__ import annotations

import json
from pathlib import Path

from core.llm import BaseLLM
from core.logging_setup import get_logger, set_agent_context
from harness.artifacts import ArtifactManager

logger = get_logger("verifier")

# 规则兜底分类特征（按 kind 区分）
_RULES = {
    "api": [
        (["timeout", "超时"], "脚本错误-超时"),
        (["assert", "期望", "expected", "assertion"], "真实Bug-断言失败"),
        (["5", "server error", "internal"], "真实Bug-接口报错"),
        (["connection", "refused", "不可达"], "环境问题-服务不可达"),
    ],
    "ui": [
        (["selector", "not found", "找不到", "定位"], "脚本错误-定位失败"),
        (["timeout", "超时"], "脚本错误-超时"),
        (["assert", "断言"], "真实Bug-UI显示错误"),
        (["模拟执行", "simulated"], "环境问题-演示模式"),
    ],
    "whitebox": [
        (["TypeError", "attribute", "import"], "脚本问题-测试代码错误"),
        (["assert", "期望"], "真实Bug-断言失败"),
        (["no test files", "未生成"], "脚本问题-用例未生成"),
    ],
}


class Verifier:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts

    def classify(self, kind: str, failed_results: list[dict]) -> list[dict]:
        """对失败用例分类：LLM 语义分类 + 规则兜底。"""
        set_agent_context("verifier")
        output: list[dict] = []
        for r in failed_results:
            category, level, reason, action = self._classify_one(kind, r)
            output.append({"name": r.get("name", ""), "category": category, "level": level,
                           "reason": reason, "action": action})
        if self.artifacts:
            self.artifacts.write("verifier", "失败分类", output, "json")
        return output

    def _classify_one(self, kind: str, result: dict) -> tuple[str, str, str, str]:
        error_text = json.dumps(result.get("error", ""), ensure_ascii=False)
        # 1) LLM 分类
        try:
            system = (Path(__file__).parent.parent / "prompts" / "verifier.md").read_text(encoding="utf-8")
            raw = self.llm.chat_json(
                system,
                f"测试类型：{kind}\n失败用例：{result.get('name')}\n错误信息：{error_text[:800]}",
            )
            category = raw.get("category", "")
            if category and category != "待确认":
                return category, raw.get("level", "bug"), raw.get("reason", ""), raw.get("action", "")
        except Exception:  # noqa: BLE001
            pass
        # 2) 规则兜底
        for markers, category in _RULES.get(kind, _RULES["api"]):
            if any(m.lower() in error_text.lower() for m in markers):
                level = "bug" if category.startswith("真实Bug") else ("flaky" if "脚本" in category else "env")
                return category, level, error_text[:200], "见报告"
        return "待确认", "unknown", error_text[:200], "人工复核"

    # ---------------- 指标计算 ----------------
    @staticmethod
    def compute_metrics(results: list[dict], coverage: float | None = None) -> dict:
        """计算质量指标（通过率/失败分布/覆盖率）。"""
        total = len(results)
        passed = sum(1 for r in results if r.get("status") == "passed")
        failed = sum(1 for r in results if r.get("status") == "failed")
        # 失败分布
        distribution: dict[str, int] = {}
        for r in results:
            if r.get("status") == "failed":
                cat = r.get("category", "待确认")
                distribution[cat] = distribution.get(cat, 0) + 1
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total * 100, 1) if total else 0.0,
            "failure_distribution": distribution,
            "coverage": coverage,
        }

    def build_html_report(self, task_info: dict, sections: dict) -> str:
        """生成 HTML 报告（第二期 Web 界面之前的独立产物）。"""
        lines = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            "<title>AI 测试报告</title>",
            "<style>body{font-family:-apple-system,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2329}",
            "h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid #eee;padding-bottom:6px}",
            "table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #e5e6eb;padding:8px 12px;font-size:13px;text-align:left}",
            ".pass{color:#00b42a}.fail{color:#f53f3f}.card{background:#f7f9fc;border-radius:8px;padding:16px;margin:12px 0}</style></head><body>",
        ]
        lines.append(f"<h1>测试报告 · {task_info.get('task_id', '')}</h1>")
        lines.append(f"<p>需求：{task_info.get('requirement', '')}</p>")
        for title, body in sections.items():
            lines.append(f"<h2>{title}</h2>{body}")
        lines.append("</body></html>")
        return "\n".join(lines)

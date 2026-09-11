"""AI 代码走查 —— 对变更代码做五维审查。"""
from __future__ import annotations

import json
from pathlib import Path

from core.llm import BaseLLM
from core.logging_setup import get_logger
from adapters.base import ChangedFunction

logger = get_logger("code_review")

# 走查结果 schema 校验（护栏：缺字段丢弃）
REQUIRED = ["severity", "location", "issue", "suggestion"]
ALLOWED = set(REQUIRED) | {"line"}


def review_changed_functions(
    llm: BaseLLM,
    changed_funcs: list[ChangedFunction],
    requirement: str = "",
    max_batch: int = 10,
) -> list[dict]:
    """对变更函数分批走查（大变更分片：每批最多 max_batch 个）。"""
    system = (Path(__file__).parent.parent / "prompts" / "whitebox_tester.md").read_text(encoding="utf-8")
    findings: list[dict] = []

    for i in range(0, len(changed_funcs), max_batch):
        batch = changed_funcs[i : i + max_batch]
        payload = [
            {
                "file": f.file_path,
                "function": f.function_name,
                "code": f.source_code[:1500],
                "signature": f.signature,
            }
            for f in batch
        ]
        user = (
            f"code_review 任务：代码走查\n"
            f"测试需求：{requirement or '（未提供）'}\n"
            f"请对以下变更函数做代码走查（需求一致性/代码规范/安全隐患/性能问题/异常处理）：\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            raw = llm.chat_json(system, user)
            items = raw if isinstance(raw, list) else raw.get("findings", raw.get("review", []))
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 走查失败，使用规则兜底: %s", e)
            items = _rule_based_review(batch)

        for item in items:
            if not isinstance(item, dict):
                continue
            # schema 校验：缺字段丢弃
            if not all(k in item for k in REQUIRED):
                continue
            item = {k: v for k, v in item.items() if k in ALLOWED}
            findings.append(item)
    return findings


def _rule_based_review(batch: list[ChangedFunction]) -> list[dict]:
    """规则兜底走查：识别常见问题模式。"""
    findings: list[dict] = []
    for f in batch:
        code = f.source_code
        if "==" in code and "hmac" not in code:
            findings.append({"severity": "严重", "location": f"{f.file_path}:{f.start_line}",
                             "issue": "检测到直接相等比较，敏感数据比较存在时序攻击风险", "suggestion": "使用常量时间比较（如 hmac.compare_digest）"})
        if "except" in code and "pass" in code:
            findings.append({"severity": "警告", "location": f"{f.file_path}:{f.start_line}",
                             "issue": "捕获异常后直接 pass，吞掉错误信息", "suggestion": "记录日志并给出可追踪的异常处理"})
        if "print(" in code:
            findings.append({"severity": "建议", "location": f"{f.file_path}:{f.start_line}",
                             "issue": "存在 print 调试输出", "suggestion": "替换为结构化日志"})
        if not findings:
            findings.append({"severity": "建议", "location": f"{f.file_path}:{f.start_line}",
                             "issue": "未发现明显问题，建议补充边界与异常场景测试", "suggestion": "覆盖空输入/超长输入/并发场景"})
    return findings

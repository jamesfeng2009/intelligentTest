"""影响面分析与风险等级评估（对齐项目计划 §3.4 第四层）。

- 上游调用者：grep + 方法名匹配（第一期实现，标注"可能有遗漏，建议人工复核"）
- 下游被调用：AST Call 节点提取
- 风险等级：调用者数量 / 变更行数 / 业务敏感词 三维规则
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from adapters.base import ChangedFunction, RiskAssessment
from adapters.factory import get_adapter
from core.logging_setup import get_logger

logger = get_logger("impact")

# 业务敏感词（涉及钱/支付/订单/权限等）
SENSITIVE_KEYWORDS = ["pay", "order", "price", "refund", "auth", "login", "permission", "token", "user", "account", "balance"]


def find_callers(repo_path: str, changed_function: str, changed_file: str, language: str = "python") -> list[str]:
    """上游调用者（简单版：grep 函数名；标注可能有遗漏）。"""
    exts = {"python": ["*.py"], "javascript": ["*.js"], "typescript": ["*.ts", "*.tsx"], "go": ["*.go"]}.get(language, ["*.py"])
    callers: set[str] = set()
    for ext in exts:
        try:
            proc = subprocess.run(
                ["grep", "-rn", "--include=" + ext, rf"{changed_function}\s*\(", repo_path],
                capture_output=True, text=True, timeout=60,
            )
            for line in proc.stdout.strip().splitlines():
                if ":" in line:
                    file_path = line.split(":")[0]
                    if file_path != changed_file:
                        callers.add(file_path)
        except Exception:  # noqa: BLE001
            continue
    return sorted(callers)


def find_downstream_calls(adapter, source: str) -> list[str]:
    """下游被调用：变更函数内部调用了哪些函数。"""
    try:
        return adapter.extract_calls(source)
    except Exception:  # noqa: BLE001
        return []


def assess_risk(func: ChangedFunction, callers: list[str], downstream: list[str] | None = None) -> RiskAssessment:
    """三维规则评估风险等级。"""
    reasons: list[str] = []
    risk = "low"
    n_callers = len(callers)
    n_lines = len(func.changed_lines)
    name = func.function_name.lower()

    if n_callers >= 5:
        risk = "high"
        reasons.append(f"被 {n_callers} 处调用，影响面大")
    elif n_callers >= 2:
        risk = "medium"
        reasons.append(f"被 {n_callers} 处调用")

    if n_lines >= 20:
        if risk == "low":
            risk = "medium"
        reasons.append(f"变更了 {n_lines} 行代码")
    elif n_lines >= 5 and risk == "low":
        reasons.append(f"变更了 {n_lines} 行代码")

    if any(kw in name for kw in SENSITIVE_KEYWORDS):
        if risk == "low":
            risk = "medium"
        reasons.append("涉及核心业务逻辑（支付/订单/权限/用户）")

    if downstream:
        reasons.append(f"内部调用 {len(downstream)} 个下游函数")

    if risk == "low":
        reasons.append("变更范围小且无敏感逻辑")
    return RiskAssessment(function=func.function_name, file=func.file_path, risk_level=risk, reasons=reasons)


def build_impact_report(repo_path: str, changed_funcs: list[ChangedFunction]) -> dict:
    """汇总影响面分析结果。"""
    report: dict = {"upstream": [], "downstream": [], "risk": [], "warning": ""}
    for func in changed_funcs:
        adapter = get_adapter(func.file_path)
        language = adapter.language if adapter else "python"
        callers = find_callers(repo_path, func.function_name, func.file_path, language)
        downstream = find_downstream_calls(adapter, func.source_code) if adapter else []
        risk = assess_risk(func, callers, downstream)
        report["upstream"].append({
            "function": func.function_name, "file": func.file_path,
            "callers": callers, "caller_count": len(callers),
        })
        report["downstream"].append({
            "function": func.function_name, "calls": downstream,
        })
        report["risk"].append(risk.to_dict())
    report["warning"] = "影响面分析基于静态代码（grep+AST），可能有遗漏，建议人工复核。"
    return report

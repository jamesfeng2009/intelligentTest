"""需求三级解析（P1）：结构化需求条目 + 追溯矩阵。

L1 文档结构解析（程序）：标题树 → sections
L2 条目识别 + L3 要素抽取（LLM + 程序校验）：→ RequirementItem[]
追溯矩阵：需求条目 ↔ 功能用例覆盖关系（供报告与缺口分析）

设计原则：LLM 只做语义抽取；字段完整性/优先级合法性/去重由程序完成；
解析产物结构化落盘，供测试计划生成 / 用例生成 / B 评审统一输入。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.llm import BaseLLM
from core.models import RequirementItem, RequirementParseResult

VALID_PRIORITY = ("P0", "P1", "P2")


# ---------------- L1：文档结构解析（程序） ----------------
def parse_document_structure(content: str) -> list[dict]:
    """提取 Markdown 标题树（标题 + 层级），作为条目来源引用。"""
    sections: list[dict] = []
    for line in (content or "").splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            sections.append({"title": m.group(2).strip(), "level": len(m.group(1))})
    return sections


# ---------------- L2/L3：LLM 抽取 + 程序校验 ----------------
def parse_requirement(content: str, llm: BaseLLM, endpoints: list[dict] | None = None,
                      extra_context: str = "") -> RequirementParseResult:
    """三级解析：返回结构化需求模型。endpoints 用于校验接口引用；extra_context 供相似需求/缺陷注入。"""
    sections = parse_document_structure(content)
    system = (Path(__file__).parent.parent / "prompts" / "requirement_parser.md").read_text(encoding="utf-8")
    user = f"requirement_parse 任务：解析以下需求为结构化条目\n测试需求：{content}\n"
    if endpoints:
        user += f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}\n"
    if extra_context:
        user += f"参考上下文（相似历史需求/用例/缺陷，供对齐口径）：\n{extra_context}\n"
    try:
        raw = llm.chat_json(system, user)
        items = _clean_items(raw.get("items", []), endpoints or [])
        result = RequirementParseResult(
            items=items,
            summary=str(raw.get("summary", "")),
            risks=[str(r) for r in (raw.get("risks", []) or [])],
            sections=sections,
        )
    except Exception as e:  # noqa: BLE001
        result = RequirementParseResult(items=[], summary=f"需求解析失败：{e}", risks=[], sections=sections)
    return result


def _clean_items(raw_items: list[Any], endpoints: list[dict]) -> list[RequirementItem]:
    """程序校验：必备字段非空、priority 合法、接口引用只保留真实端点、按 id 去重。"""
    endpoint_map = {(e["method"].upper(), e["path"]): True for e in endpoints}
    out: list[RequirementItem] = []
    seen: set[str] = set()
    for i, it in enumerate(raw_items):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        if not title or title in seen:
            continue
        eps = []
        for ep in it.get("involved_endpoints", []) or []:
            if isinstance(ep, dict):
                m = str(ep.get("method", "")).upper()
                p = str(ep.get("path", ""))
                if (m, p) in endpoint_map:
                    eps.append({"method": m, "path": p})
        priority = str(it.get("priority", "P1"))
        if priority not in VALID_PRIORITY:
            priority = "P1"
        item = RequirementItem(
            id=str(it.get("id") or f"RQ-{i + 1:03d}"),
            title=title,
            desc=str(it.get("desc", "")).strip(),
            acceptance=[str(a) for a in (it.get("acceptance", []) or []) if str(a).strip()],
            rules=[str(r) for r in (it.get("rules", []) or []) if str(r).strip()],
            constraints=[str(c) for c in (it.get("constraints", []) or []) if str(c).strip()],
            priority=priority,
            involved_endpoints=eps,
            source=str(it.get("source", "")).strip(),
        )
        seen.add(title)
        out.append(item)
    return out


# ---------------- 追溯矩阵 ----------------
def build_traceability(items: list[RequirementItem], cases: list[dict]) -> list[dict]:
    """需求条目 ↔ 功能用例覆盖关系。匹配：用例 feature 或 traceability.requirement 与条目标题相关。"""
    rows: list[dict] = []
    for it in items:
        covered: list[str] = []
        for c in cases:
            feature = str(c.get("feature", ""))
            req_trace = str(c.get("traceability", {}).get("requirement", ""))
            # 标题包含 / 标题被包含 / 需求追溯命中，任一即算覆盖
            if (it.title and (it.title in feature or feature in it.title or it.title in req_trace)):
                covered.append(str(c.get("id", "?")))
        rows.append({
            "item_id": it.id, "item_title": it.title, "priority": it.priority,
            "covered_cases": covered, "status": "covered" if covered else "gap",
        })
    return rows


def render_traceability_md(rows: list[dict]) -> str:
    lines = ["# 需求-用例追溯矩阵", "", "| 需求条目 | 优先级 | 覆盖用例 | 状态 |", "| --- | --- | --- | --- |"]
    for r in rows:
        cases = ", ".join(r["covered_cases"]) or "-"
        mark = "✅ 已覆盖" if r["status"] == "covered" else "❌ 未覆盖（缺口）"
        lines.append(f"| {r['item_id']} {r['item_title']} | {r['priority']} | {cases} | {mark} |")
    gaps = [r for r in rows if r["status"] == "gap"]
    lines.append("")
    lines.append(f"## 覆盖缺口（{len(gaps)}）")
    for g in gaps:
        lines.append(f"- {g['item_id']} {g['item_title']}：无用例覆盖，建议补充")
    return "\n".join(lines)


def render_requirement_md(result: RequirementParseResult) -> str:
    lines = ["# 需求解析（结构化条目）", "", f"- 摘要：{result.summary or '-'}", f"- 功能点：{len(result.items)} 个"]
    if result.risks:
        lines.append(f"- 风险：{'；'.join(result.risks)}")
    lines.append("")
    lines.append("| ID | 功能点 | 描述 | 验收标准 | 业务规则 | 约束 | 优先级 | 涉及接口 | 来源 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for it in result.items:
        eps = "; ".join(f"{e['method']} {e['path']}" for e in it.involved_endpoints) or "-"
        lines.append(f"| {it.id} | {it.title} | {it.desc[:60]} | {'<br>'.join(it.acceptance) or '-'} "
                     f"| {'<br>'.join(it.rules) or '-'} | {'<br>'.join(it.constraints) or '-'} | {it.priority} | {eps} | {it.source or '-'} |")
    return "\n".join(lines)

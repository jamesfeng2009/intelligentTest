"""需求三级解析（P1）+ 分层多趟提取（A3）：结构化需求条目 + 追溯矩阵。

L1 文档结构解析（程序）：标题树 → sections
L2/L3 条目 + 要素提取（LLM + 程序校验）：→ RequirementItem[]

A3 分层多趟提取（防上下文衰减/本能幻觉）：
    Pass 1 功能面：只输出【功能点清单】+ 摘要
    Pass 2 规则面：只输出【字段约束表】（rules / constraints）
    Pass 3 依赖面：只输出【上下游接口契约 + 数据兼容规则】（endpoints / upstream / downstream / data_compat）
    Pass 4 异常面：只输出【异常与边界】（risks / edge_cases / anomalies）
每趟只盯一件事，最终按条目 id（回退标题）合并去重；强依据追溯，需求模糊时必须输出风险。

设计原则：LLM 只做语义抽取；字段完整性/优先级合法性/去重由程序完成；
解析产物结构化落盘，供测试计划生成 / 用例生成 / B 评审统一输入。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.llm import BaseLLM
from core.logging_setup import get_logger
from core.models import RequirementItem, RequirementParseResult

logger = get_logger("requirement_parser")

VALID_PRIORITY = ("P0", "P1", "P2")

# A3 分层多趟提取：每趟的定义（聚焦指令 + 输出 schema），由 parse_requirement 依次调用
PASS_SPECS: list[dict] = [
    {
        "no": 1, "name": "功能面", "marker": "requirement_parse_pass1",
        "focus": ("本轮只提取【功能面】：逐条输出功能点 id/title/desc/acceptance/priority/source/est_cases。"
                  "est_cases 为预估用例数：正常/异常/边界/权限至少各 1 条，按功能复杂度估算。"
                  "不要输出规则、约束、接口、风险（后续趟次负责）。"),
        "schema": ('{"summary": "一句话摘要", "items": [{"id": "RQ-001", "title": "...", "desc": "...", '
                   '"acceptance": ["可判定验收标准"], "priority": "P0|P1|P2", "source": "章节", "est_cases": 4}]}'),
    },
    {
        "no": 2, "name": "规则面", "marker": "requirement_parse_pass2",
        "focus": ("本轮只提取【规则面：字段约束表】。按【Pass1 条目清单】中的 id 逐条补充 rules（业务规则）"
                  "与 constraints（字段长度/格式/边界/权限/超时等显式与隐性约束）。不要重复功能点描述与验收标准。"),
        "schema": '{"items": [{"id": "RQ-001", "rules": ["..."], "constraints": ["..."]}]}',
    },
    {
        "no": 3, "name": "依赖面", "marker": "requirement_parse_pass3",
        "focus": ("本轮只提取【依赖面：上下游接口契约 + 数据兼容规则】。按【Pass1 条目清单】中的 id 逐条输出："
                  "involved_endpoints（只能来自接口清单，method+path 必须与清单一致）、upstream_systems（上游系统/服务）、"
                  "downstream_systems（下游系统/服务）、data_compat_rules（数据兼容规则：字段格式、枚举取值、兼容旧数据）。"),
        "schema": ('{"items": [{"id": "RQ-001", "involved_endpoints": [{"method": "POST", "path": "/api/v1/..."}], '
                   '"upstream_systems": [], "downstream_systems": [], "data_compat_rules": []}]}'),
    },
    {
        "no": 4, "name": "异常面", "marker": "requirement_parse_pass4",
        "focus": ("本轮只提取【异常面：异常与边界】。输出 risks（需求描述模糊/缺失/歧义、无法判定的风险——"
                  "需求描述模糊或缺失时必须输出风险条目）、edge_cases（边界值与异常场景）、"
                  "anomalies（异常处理与容错要求）。不要重复功能点。"),
        "schema": '{"risks": ["..."], "edge_cases": ["..."], "anomalies": ["..."]}',
    },
]


# ---------------- L1：文档结构解析（程序） ----------------
def parse_document_structure(content: str) -> list[dict]:
    """提取 Markdown 标题树（标题 + 层级），作为条目来源引用。"""
    sections: list[dict] = []
    for line in (content or "").splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            sections.append({"title": m.group(2).strip(), "level": len(m.group(1))})
    return sections


# ---------------- A3 分层多趟提取（L2/L3：LLM 抽取 + 程序校验 + 合并） ----------------
def parse_requirement(content: str, llm: BaseLLM, endpoints: list[dict] | None = None,
                      extra_context: str = "") -> RequirementParseResult:
    """多趟提取：Pass1 功能面 → Pass2 规则面 → Pass3 依赖面 → Pass4 异常面，逐趟合并去重。

    endpoints 用于校验接口引用；extra_context 供相似需求/缺陷注入（Pass1/Pass3 使用）。
    """
    sections = parse_document_structure(content)
    base_system = (Path(__file__).parent.parent / "prompts" / "requirement_parser.md").read_text(encoding="utf-8")
    endpoint_json = json.dumps(endpoints or [], ensure_ascii=False)
    pass_report: dict[str, int] = {}

    # ---- Pass 1 功能面（必须成功；失败回退单趟直读） ----
    raw1: dict = {}
    try:
        sys1 = _pass_system(base_system, PASS_SPECS[0])
        user1 = _pass_user(1, content, endpoint_json, extra_context=extra_context,
                           pass1_items=None)
        raw1 = llm.chat_json(sys1, user1)
    except Exception as e:  # noqa: BLE001
        raw1 = {}
        logger.warning("Pass1 功能面提取失败，回退单趟直读: %s", e)
        raw1 = _fallback_single_pass(llm, base_system, content, endpoint_json, extra_context)
    items = _clean_items(raw1.get("items", []), endpoints or [])
    summary = str(raw1.get("summary", ""))
    pass_report["Pass1功能面"] = len(items)

    # ---- Pass 2 规则面 ----
    raw2: dict = {}
    if items:
        try:
            raw2 = llm.chat_json(_pass_system(base_system, PASS_SPECS[1]),
                                 _pass_user(2, content, endpoint_json, pass1_items=items))
        except Exception as e:  # noqa: BLE001
            logger.warning("Pass2 规则面提取失败，跳过（rules/constraints 保持 Pass1 值）: %s", e)
        p2n = len(_index_pass_items(raw2.get("items", [])))
        pass_report["Pass2规则面"] = p2n

    # ---- Pass 3 依赖面 ----
    raw3: dict = {}
    if items:
        try:
            raw3 = llm.chat_json(_pass_system(base_system, PASS_SPECS[2]),
                                 _pass_user(3, content, endpoint_json, extra_context=extra_context,
                                            pass1_items=items))
        except Exception as e:  # noqa: BLE001
            logger.warning("Pass3 依赖面提取失败，跳过（endpoints/上下游保持 Pass1 值）: %s", e)
        p3n = len(_index_pass_items(raw3.get("items", [])))
        pass_report["Pass3依赖面"] = p3n

    # ---- Pass 4 异常面 ----
    raw4: dict = {}
    try:
        raw4 = llm.chat_json(_pass_system(base_system, PASS_SPECS[3]),
                             _pass_user(4, content, endpoint_json, pass1_items=items))
    except Exception as e:  # noqa: BLE001
        logger.warning("Pass4 异常面提取失败，跳过（risks 保持默认）: %s", e)
    risks = [str(r) for r in (raw4.get("risks", []) or []) if str(r).strip()]
    edge_cases = [str(x) for x in (raw4.get("edge_cases", []) or []) if str(x).strip()]
    anomalies = [str(x) for x in (raw4.get("anomalies", []) or []) if str(x).strip()]
    # 强依据追溯：需求描述模糊时强制保留风险（源约束 A4 兜底）
    if not risks:
        risks = [str(r) for r in (raw1.get("risks", []) or []) if str(r).strip()]
    pass_report["Pass4异常面"] = len(risks) + len(edge_cases) + len(anomalies)

    # ---- 合并去重 ----
    _merge_passes(items, raw2.get("items", []), raw3.get("items", []), endpoints or [])
    # ---- A2/A4/A15 程序级强制：目录完成度 / 源约束风险 / 预估用例数兜底 ----
    est_risks = _enforce_source_and_estimate(items, raw1.get("items", []), content, risks)
    sections = _mark_section_coverage(sections, items)
    risks = _dedupe(risks + est_risks)
    result = RequirementParseResult(
        items=items,
        summary=summary,
        risks=risks,
        sections=sections,
        edge_cases=_dedupe(edge_cases),
        anomalies=_dedupe(anomalies),
        pass_report=pass_report,
    )
    return result


# ---------------- A2 目录驱动提取 / A4 源约束强制 / A15 预估用例数 ----------------
def _enforce_source_and_estimate(items: list[RequirementItem], raw1_items: list[Any],
                                 requirement: str, existing_risks: list[str]) -> list[str]:
    """程序级强制（A4 源约束 + A15 预估用例数兜底）：

    - est_cases 缺失/非法 → 按复杂度确定性估算（至少 1 条）
    - 验收标准缺失 → 强制产出风险（无法判定可观察结果）
    - 来源章节缺失 → 强制产出风险（无法向上追溯）
    - 需求含模糊措辞且无任何风险 → 强制产出复核风险
    """
    extra_risks: list[str] = []
    raw_map = {str(r.get("id", "")): r for r in raw1_items if isinstance(r, dict) and r.get("id")}
    for it in items:
        # A15 预估用例数：LLM 给了合法正整数则保留，否则确定性兜底估算
        raw = raw_map.get(it.id, {})
        try:
            est = int(raw.get("est_cases", 0) or 0)
        except (TypeError, ValueError):
            est = 0
        if est < 1:
            est = max(1, len(it.acceptance) + (1 if it.rules else 0) + (1 if it.constraints else 0))
        it.est_cases = est
        # A4 源约束：验收标准缺失
        if not it.acceptance:
            extra_risks.append(f"【源约束】{it.id} {it.title}: 验收标准缺失，无法判定可观察结果")
        # A4 源约束：来源章节缺失
        if not it.source:
            extra_risks.append(f"【源约束】{it.id} {it.title}: 来源章节缺失，无法向上追溯需求")
    # A4 源约束：需求含模糊措辞且全程无风险时，强制复核
    fuzzy = [w for w in ("等", "适当", "尽快", "相关", "必要时", "建议", "尽量", "酌情") if w in requirement]
    if fuzzy and not existing_risks and not extra_risks:
        extra_risks.append(f"【源约束】需求描述含模糊措辞（{'、'.join(fuzzy)}），建议人工复核需求原文")
    return extra_risks


def _mark_section_coverage(sections: list[dict], items: list[RequirementItem]) -> list[dict]:
    """A2 目录驱动提取：对照文档目录，逐章节标记是否已提取到功能点（含缺口提示）。"""
    out: list[dict] = []
    for sec in sections:
        title = sec["title"]
        hit = [it for it in items if it.source and (it.source.strip() == title.strip()
                                                    or title.strip() in it.source or it.source.strip() in title.strip())]
        if hit:
            sec = {**sec, "extracted": True, "item_ids": [it.id for it in hit]}
        else:
            sec = {**sec, "extracted": False, "item_ids": [], "gap": "未提取到功能点"}
        out.append(sec)
    return out


def _pass_system(base_system: str, spec: dict) -> str:
    """拼接每趟的 system：全局角色 + 本趟聚焦指令 + 输出 schema。"""
    return (
        f"{base_system}\n\n"
        f"# 本轮任务（Pass {spec['no']}：{spec['name']}）\n"
        f"{spec['focus']}\n"
        f"# 本轮输出 schema\n{spec['schema']}\n"
        f"JSON 对象，不要输出其它内容。"
    )


def _pass_user(pass_no: int, content: str, endpoint_json: str, extra_context: str = "",
               pass1_items: list[RequirementItem] | None = None) -> str:
    """拼接每趟的 user prompt：携带趟次标记（供 mock 路由）+ 需求原文 + 接口清单 + Pass1 条目。"""
    spec = PASS_SPECS[pass_no - 1]
    user = f"{spec['marker']} 任务：需求分层多趟提取（Pass {spec['no']}：{spec['name']}）\n测试需求：{content}\n"
    if endpoint_json and endpoint_json != "[]":
        user += f"接口清单：{endpoint_json}\n"
    if pass1_items is not None:
        brief = [{"id": it.id, "title": it.title} for it in pass1_items]
        user += f"【Pass1 条目清单（按此 id 对齐，不得新增条目）】{json.dumps(brief, ensure_ascii=False)}\n"
    if extra_context:
        user += f"参考上下文（相似历史需求/用例/缺陷，供对齐口径）：\n{extra_context}\n"
    return user


def _fallback_single_pass(llm: BaseLLM, base_system: str, content: str, endpoint_json: str,
                          extra_context: str) -> dict:
    """Pass1 失败时的兼容回退：单趟直读（保留旧标记，mock 可路由）。"""
    user = f"requirement_parse 任务：解析以下需求为结构化条目\n测试需求：{content}\n"
    if endpoint_json and endpoint_json != "[]":
        user += f"接口清单：{endpoint_json}\n"
    if extra_context:
        user += f"参考上下文（相似历史需求/用例/缺陷，供对齐口径）：\n{extra_context}\n"
    return llm.chat_json(base_system, user)


def _index_pass_items(raw_items: list[Any]) -> dict[str, dict]:
    """按 id 索引一趟的 items（真实 LLM 可能 id 漂移，保留原始以做标题回退匹配）。"""
    out: dict[str, dict] = {}
    for it in raw_items or []:
        if isinstance(it, dict) and it.get("id"):
            out[str(it["id"])] = it
    return out


def _merge_passes(items: list[RequirementItem], raw2: list[Any], raw3: list[Any],
                  endpoints: list[dict]) -> None:
    """把 Pass2（规则面）/Pass3（依赖面）合并进 Pass1 条目：按 id 匹配，回退标题匹配，逐字段去重。"""
    ep_map = {(e["method"].upper(), e["path"]): True for e in endpoints}
    p2 = _index_pass_items(raw2)
    p3 = _index_pass_items(raw3)

    def _match(idx: dict[str, dict], it: RequirementItem) -> dict | None:
        if it.id in idx:
            return idx[it.id]
        for v in idx.values():
            if str(v.get("title", "")).strip() == it.title:
                return v
        return None

    for it in items:
        # Pass2 规则面
        k2 = _match(p2, it)
        if k2:
            it.rules = _dedupe(it.rules + [str(r) for r in (k2.get("rules", []) or []) if str(r).strip()])
            it.constraints = _dedupe(it.constraints + [str(c) for c in (k2.get("constraints", []) or []) if str(c).strip()])
        # Pass3 依赖面
        k3 = _match(p3, it)
        if k3:
            eps = []
            for ep in k3.get("involved_endpoints", []) or []:
                if not isinstance(ep, dict):
                    continue
                m = str(ep.get("method", "")).upper()
                p = str(ep.get("path", ""))
                if (m, p) in ep_map:
                    eps.append({"method": m, "path": p})
            it.involved_endpoints = _dedupe_eps(it.involved_endpoints + eps)
            it.upstream_systems = _dedupe(it.upstream_systems + [str(s) for s in (k3.get("upstream_systems", []) or []) if str(s).strip()])
            it.downstream_systems = _dedupe(it.downstream_systems + [str(s) for s in (k3.get("downstream_systems", []) or []) if str(s).strip()])
            it.data_compat_rules = _dedupe(it.data_compat_rules + [str(r) for r in (k3.get("data_compat_rules", []) or []) if str(r).strip()])


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _dedupe_eps(eps: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for e in eps:
        k = (e["method"].upper(), e["path"])
        if k not in seen:
            seen.add(k)
            out.append({"method": k[0], "path": k[1]})
    return out


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
    lines = ["# 需求解析（结构化条目 · A3 分层多趟提取）", "", f"- 摘要：{result.summary or '-'}", f"- 功能点：{len(result.items)} 个"]
    if result.risks:
        lines.append(f"- 风险：{'；'.join(result.risks)}")
    if result.pass_report:
        pr = "；".join(f"{k}:{v}" for k, v in result.pass_report.items())
        lines.append(f"- 多趟提取：{pr}")
    lines.append("")
    # A2 目录驱动提取：章节完成度对照
    lines.append("## 目录完成度对照（A2）")
    lines.append("| 章节 | 层级 | 是否提取 | 覆盖条目 |")
    lines.append("| --- | --- | --- | --- |")
    for s in result.sections:
        mark = "✅" if s.get("extracted") else f"❌ {s.get('gap', '未提取')}"
        ids = "、".join(s.get("item_ids", [])) or "-"
        lines.append(f"| {s['title']} | {s['level']} | {mark} | {ids} |")
    lines.append("")
    lines.append("| ID | 功能点 | 描述 | 验收标准 | 业务规则 | 约束 | 优先级 | 预估用例数 | 涉及接口 | 来源 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for it in result.items:
        eps = "; ".join(f"{e['method']} {e['path']}" for e in it.involved_endpoints) or "-"
        lines.append(f"| {it.id} | {it.title} | {it.desc[:60]} | {'<br>'.join(it.acceptance) or '-'} "
                     f"| {'<br>'.join(it.rules) or '-'} | {'<br>'.join(it.constraints) or '-'} | {it.priority} "
                     f"| {it.est_cases or '-'} | {eps} | {it.source or '-'} |")
    # A3 Pass3 依赖面
    if any(it.upstream_systems or it.downstream_systems or it.data_compat_rules for it in result.items):
        lines.append("")
        lines.append("## 依赖面（Pass3：上下游接口契约 + 数据兼容规则）")
        lines.append("| ID | 功能点 | 上游系统/服务 | 下游系统/服务 | 数据兼容规则 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for it in result.items:
            up = "<br>".join(it.upstream_systems) or "-"
            down = "<br>".join(it.downstream_systems) or "-"
            compat = "<br>".join(it.data_compat_rules) or "-"
            lines.append(f"| {it.id} | {it.title} | {up} | {down} | {compat} |")
    # A3 Pass4 异常面
    if result.edge_cases or result.anomalies:
        lines.append("")
        lines.append("## 异常与边界（Pass4）")
        if result.edge_cases:
            lines.append("- **边界与异常场景**：" + "；".join(result.edge_cases))
        if result.anomalies:
            lines.append("- **异常处理与容错**：" + "；".join(result.anomalies))
    return "\n".join(lines)

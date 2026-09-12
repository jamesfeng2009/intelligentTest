"""交付链路聚合（Harness Inspector 借鉴 A）。

把一次测试任务聚合为可追溯的交付证据视图：
    Intent(需求) → Process(状态机 / 审批决议 / Trace) → Output(报告 / 失败分类 / 缺陷条目)

证据诚实（Inspector 借鉴 B 的思想）：
- linked    程序强制关联（需求→任务、任务→报告、任务→trace、失败→分类）
- candidate 证据不足（失败用例缺分类、任务无缺陷关联）——保留为候选而非自动补全
- unmapped  完全未映射（如任务进行中尚无 trace）

数据来源（按优先级）：
1. artifacts/runs/<task_id>/orchestrator/task_result.json —— 完整执行 out（含 approvals/trace/state）
2. Task.result —— 归一化结果（runner 持久化）
3. eval.trace.query_trace —— Trace 双写（DB/JSONL，支持折叠）
4. Report / KnowledgeDoc 表 —— 报告与缺陷条目
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from web.models import KnowledgeDoc, Report

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_RUNS = PROJECT_ROOT / "artifacts" / "runs"


def _read_json(path: Path) -> dict | None:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 产物损坏不阻断
        return None
    return None


def _resolve_run_dir(task_row) -> Path | None:
    """定位任务产物目录：result.artifacts_dir 优先，回退 artifacts/runs/<trace_id>。"""
    result = task_row.result or {}
    d = result.get("artifacts_dir") or ""
    if d:
        p = Path(d)
        if p.is_dir():
            return p
    trace_id = task_row.trace_id or f"task_{task_row.id}"
    p = ARTIFACTS_RUNS / trace_id
    return p if p.is_dir() else None


def _flatten_results(result: dict) -> list[dict]:
    """兼容两种 results 形态：orchestrator dict{kind: {results:[]}} 与单线 list[]。"""
    out: list[dict] = []
    results = result.get("results")
    if isinstance(results, dict):
        for kind, v in results.items():
            if not isinstance(v, dict):
                continue
            for r in v.get("results", []):
                if isinstance(r, dict):
                    item = dict(r)
                    item.setdefault("kind", kind)
                    out.append(item)
    elif isinstance(results, list):
        for r in results:
            if isinstance(r, dict):
                out.append(dict(r))
    return out


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 60, 2)


# ---------------- 聚合 ----------------
def build_delivery(db: Session, task_row) -> dict:
    """构建一次任务的交付链路证据视图。"""
    result = task_row.result or {}
    run_dir = _resolve_run_dir(task_row)
    full_out: dict | None = None
    if run_dir:
        full_out = _read_json(run_dir / "orchestrator" / "task_result.json")
        if full_out is None:
            # 非 orchestrator 任务：产物可能直接以 agent 子目录组织，取 run 级兜底
            full_out = _read_json(run_dir / "task_result.json")

    # ---------- Intent ----------
    req_items: list[dict] = []
    intent_source = "task_only"
    if run_dir:
        req_doc = _read_json(run_dir / "orchestrator" / "需求解析.json")
        if req_doc and isinstance(req_doc, dict):
            req_items = req_doc.get("items") or []
            intent_source = "artifacts"

    # ---------- Process ----------
    state_machine = (full_out or {}).get("trace") or result.get("trace") or []
    approvals = (full_out or {}).get("approvals") or result.get("approvals") or []
    trace_id = task_row.trace_id or f"task_{task_row.id}"
    from eval.trace import query_trace

    trace = query_trace(db, trace_id, collapse=True)
    if trace.get("error"):
        trace["steps"] = []
        trace.pop("error", None)

    effort = {
        "delivery_min": _minutes(task_row.created_at, task_row.finished_at),
        "execution_min": _minutes(task_row.started_at, task_row.finished_at),
        "waiting_min": None,
        "note": "环节级耗时（审批等待/各阶段）依赖三期③时间戳补齐后提供",
    }

    # ---------- Output ----------
    reports = [{
        "id": r.id, "report_type": r.report_type, "summary": r.summary,
        "passed": r.passed, "failed": r.failed, "total": r.total,
        "passed_rate": r.passed_rate, "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in db.query(Report).filter_by(task_id=task_row.id).order_by(Report.id).all()]

    failed_cases: list[dict] = []
    for r in _flatten_results(result):
        if r.get("status") != "failed":
            continue
        category = r.get("category")
        failed_cases.append({
            "kind": r.get("kind", "-"),
            "name": r.get("name", "-"),
            "error": (r.get("error") or "")[:300],
            "category": category,
            "evidence": "linked" if category else "candidate",
        })

    from web.models import KnowledgeDoc

    defect_docs = (db.query(KnowledgeDoc)
                   .filter(KnowledgeDoc.project_id == task_row.project_id,
                           KnowledgeDoc.doc_type == "defects")
                   .order_by(KnowledgeDoc.id.desc()).limit(20).all())
    defects = [{
        "id": d.id, "name": d.name, "status": d.status,
        "meta": d.meta, "created_at": d.created_at.isoformat() if d.created_at else None,
    } for d in defect_docs]
    # 只保留与本任务关联的缺陷（meta.task_id）；无关联时诚实标记 candidate
    linked_defects = [d for d in defects if str(d["meta"].get("task_id", "")) == str(task_row.id)]

    # ---------- 证据诚实 ----------
    linked = []
    candidates = []
    if task_row.requirement:
        linked.append("intent→process：需求已挂接到任务")
    if state_machine:
        linked.append("process：状态机转移记录存在")
    if approvals:
        linked.append("process：审批决议记录存在")
    if trace.get("steps"):
        linked.append("process→trace：Trace 步骤可回放")
    if reports:
        linked.append("process→output：任务关联报告")
    if any(fc["evidence"] == "linked" for fc in failed_cases):
        linked.append("output：失败用例已分类（真实Bug/脚本错误）")
    if not state_machine and not trace.get("steps"):
        candidates.append("process：状态机/Trace 未记录（任务进行中或产物缺失）")
    for fc in failed_cases:
        if fc["evidence"] == "candidate":
            candidates.append(f"失败用例「{fc['name']}」缺分类，保留为候选（建议人工复核）")
    if not linked_defects and defect_docs:
        candidates.append("output：存在缺陷库文档但无本任务关联条目（③④缺陷闭环未写回）")
    elif not linked_defects and not defect_docs:
        candidates.append("output：尚无缺陷库条目（③④缺陷知识闭环未落地）")

    return {
        "task_id": task_row.id,
        "intent": {
            "requirement": task_row.requirement or "",
            "requirement_items": req_items,
            "items_count": len(req_items),
            "source": intent_source,
        },
        "process": {
            "state_machine": state_machine,
            "approvals": approvals,
            "trace": trace,
            "effort": effort,
        },
        "output": {
            "summary": {
                "status": result.get("status", task_row.status),
                "summary": result.get("summary", ""),
                "passed": result.get("passed", 0),
                "failed": result.get("failed", 0),
                "total": result.get("total", 0),
                "passed_rate": result.get("passed_rate", 0.0),
            },
            "reports": reports,
            "failed_cases": failed_cases,
            "defects": linked_defects,
            "defect_pool_total": len(defect_docs),
        },
        "mapping_evidence": {
            "linked": linked,
            "candidates": candidates,
            "unmapped": [],
        },
    }

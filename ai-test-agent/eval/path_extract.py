"""工作路径聚合（Harness Inspector 借鉴 C）。

从多次测试任务中提取稳定工作路径（状态机序列 + 审批决议 + 失败分类），
输出"稳定路径"清单作为 SKILL / 模板沉淀的候选输入——
对应 Inspector 文章的核心论点：从多次真实交付中识别稳定模式，
而不是把单次 Session 总结成 SKILL。

路径签名示例（可比较）：
    orchestrator|ANALYZE>WAIT_APPROVAL>SETUP>API_TEST>VERIFY>REPORT>DONE|计划审批:approved|真实Bug-数据不一致

证据诚实：仅聚合已持久化的数据（Task.result 的 trace/approvals/分类）；
缺失字段不推断，签名中省略。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from web.models import Task


def _flatten_failed_categories(result: dict) -> list[str]:
    """提取所有失败用例的分类（去重保序）。兼容 dict{kind:{results:[]}} 与 list[]。"""
    cats: list[str] = []
    seen: set[str] = set()
    results = (result or {}).get("results")
    items: list[dict] = []
    if isinstance(results, dict):
        for v in results.values():
            if isinstance(v, dict):
                items.extend(x for x in v.get("results", []) if isinstance(x, dict))
    elif isinstance(results, list):
        items = [x for x in results if isinstance(x, dict)]
    for r in items:
        if r.get("status") == "failed":
            c = r.get("category")
            if c and c not in seen:
                seen.add(c)
                cats.append(c)
    return cats


def task_path(task_row) -> dict:
    """单任务的路径签名（状态机序列 + 审批决议 + 失败分类）。"""
    result = task_row.result or {}
    state_path = [s.get("to") for s in (result.get("trace") or []) if isinstance(s, dict)]
    approvals = [(a.get("point", "?"), a.get("decision", "?"), a.get("reviewer", "?"))
                 for a in (result.get("approvals") or []) if isinstance(a, dict)]
    approval_str = ",".join(f"{p}:{d}" for p, d, _ in approvals)
    categories = _flatten_failed_categories(result)
    sig = "|".join([
        task_row.task_type or "?",
        ">".join(state_path) if state_path else "-",
        approval_str or "-",
        ",".join(categories) or "-",
    ])
    return {
        "task_id": task_row.id,
        "task_type": task_row.task_type,
        "status": task_row.status,
        "signature": sig,
        "state_path": state_path,
        "approvals": approvals,
        "categories": categories,
        "human_gates": sum(1 for _, _, reviewer in approvals if reviewer == "human"),
        "finished_at": task_row.finished_at.isoformat() if task_row.finished_at else None,
    }


def cluster_stable_paths(paths: list[dict], min_count: int = 2) -> list[dict]:
    """按签名聚类多次任务 → 稳定路径统计。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        groups[p["signature"]].append(p)
    out = []
    for sig, items in groups.items():
        if len(items) < min_count:
            continue
        done = sum(1 for p in items if p["status"] == "done")
        cats = sorted({c for p in items for c in p["categories"]})
        approvers = sorted({f"{point}:{decision}" for item in items for point, decision, _ in item["approvals"]})
        out.append({
            "signature": sig,
            "count": len(items),
            "success_rate": round(done / len(items), 3),
            "task_ids": sorted(p["task_id"] for p in items),
            "categories": cats,
            "human_gates_avg": round(sum(p["human_gates"] for p in items) / len(items), 2),
            "first_seen": min(p["finished_at"] or "" for p in items),
            "last_seen": max(p["finished_at"] or "" for p in items),
        })
    out.sort(key=lambda x: (-x["count"], -x["success_rate"]))
    return out


def list_stable_paths(db: Session, project_id: int | None = None, min_count: int = 2,
                      limit: int = 20) -> list[dict]:
    """查询稳定工作路径清单（SKILL/模板候选输入）。

    min_count=1 时返回全部路径（含单次，便于先看分布）；默认 ≥2 视为"重复出现"。
    """
    q = db.query(Task).filter(Task.status.in_(["done", "failed"]))
    if project_id:
        q = q.filter_by(project_id=project_id)
    paths = [task_path(t) for t in q.order_by(Task.id.desc()).limit(500).all()]
    return cluster_stable_paths(paths, min_count=min_count)[:limit]

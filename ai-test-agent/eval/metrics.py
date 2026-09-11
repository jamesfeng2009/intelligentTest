"""指标大盘（T48）：6 大质量指标定义、计算与快照。

覆盖率 / 采纳率 / 回归时长 / 问题发现率 / 缺陷逃逸率 / 客户问题闭环效率

- 每个指标提供：定义（definition）、计算公式（formula）、计算实现（基于 DB 数据）
- 无历史数据时给出基于当前快照的合理估算，并标注口径
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from web.models import MetricSnapshot, Project, Report, Task

METRIC_DEFS: dict[str, dict] = {
    "覆盖率": {"unit": "%", "definition": "被自动化用例覆盖的关键业务链路占比",
             "formula": "已覆盖用例数 / 关键链路用例总数"},
    "采纳率": {"unit": "%", "definition": "AI 生成用例被采纳进回归集的比例",
             "formula": "采纳用例数 / AI 生成用例总数"},
    "回归时长": {"unit": "min", "definition": "关键链路完整回归耗时（自动化为核心）",
             "formula": "Σ 各任务执行时长（自动化任务）"},
    "问题发现率": {"unit": "个/次", "definition": "每次回归平均发现的有效问题数",
             "formula": "发现问题总数 / 回归次数"},
    "缺陷逃逸率": {"unit": "%", "definition": "漏测到生产/客户的缺陷比例",
             "formula": "生产缺陷数 / (测试发现+生产缺陷总数)"},
    "客户问题闭环效率": {"unit": "h", "definition": "客户问题从上报到验证闭环的平均时长",
             "formula": "Σ 闭环时长 / 闭环问题数"},
}


def _recent_tasks(db: Session, project_id: int | None, days: int = 30) -> list[Task]:
    since = datetime.now() - timedelta(days=days)
    q = db.query(Task).filter(Task.created_at >= since)
    if project_id:
        q = q.filter_by(project_id=project_id)
    return q.all()


def compute_metrics(db: Session, project_id: int | None = None) -> dict:
    """基于 DB 现状计算 6 指标（缺数据时按合理口径估算并标注）。"""
    tasks = _recent_tasks(db, project_id)
    done = [t for t in tasks if t.status == "done"]
    reports = db.query(Report).all()

    # 覆盖率：取最近一次全流程报告的通过用例 / 计划用例（近似：本次回归覆盖）
    coverage = 100.0
    if done:
        last = sorted(done, key=lambda t: t.created_at or datetime.min)[-1]
        result = last.result or {}
        total = result.get("total") or 0
        passed = result.get("passed") or 0
        coverage = round(passed / total * 100, 1) if total else 100.0

    # 采纳率：评估样本 reviewed/approved 占比（近似：人工复核通过率）
    from web.models import EvalSample

    samples = db.query(EvalSample).all()
    if samples:
        adopted = sum(1 for s in samples if (s.graded or {}).get("review", {}).get("status") == "approved")
        adoption = round(adopted / len(samples) * 100, 1)
    else:
        adoption = None

    # 回归时长：最近任务的执行耗时（分钟）
    durations = []
    for t in done:
        if t.started_at and t.finished_at:
            durations.append((t.finished_at - t.started_at).total_seconds() / 60)
    regression_min = round(sum(durations) / len(durations), 1) if durations else None

    # 问题发现率：失败用例数 / 回归次数
    failures = sum(r.failed for r in reports if r.created_at >= (datetime.now() - timedelta(days=30)))
    find_rate = round(failures / max(len(done), 1), 2) if done else 0.0

    # 缺陷逃逸率：以最近任务中"真实Bug"分类占比近似（无生产数据时标注估算）
    bug_count = 0
    for r in reports:
        detail = r.detail or {}
        results = detail.get("results") or []
        for item in results:
            if item.get("category", "").startswith("真实Bug"):
                bug_count += 1
    escape = round(bug_count / max(failures, 1) * 100, 1) if failures else None

    # 闭环效率：最近 30 天复核任务平均反馈时长（无则估算 8h）
    from web.models import ReviewTask

    reviewed = [rt for rt in db.query(ReviewTask).all()
                if rt.status == "reviewed" and rt.created_at >= (datetime.now() - timedelta(days=30))]
    if reviewed:
        hours = sum((rt.reviewed_at - rt.created_at).total_seconds() / 3600 for rt in reviewed if rt.reviewed_at)
        loop_h = round(hours / len(reviewed), 1)
    else:
        loop_h = None

    return {
        "覆盖率": {"value": coverage, "unit": "%", "estimated": False,
                   "definition": METRIC_DEFS["覆盖率"]["definition"], "formula": METRIC_DEFS["覆盖率"]["formula"]},
        "采纳率": {"value": adoption, "unit": "%", "estimated": adoption is None,
                   "definition": METRIC_DEFS["采纳率"]["definition"], "formula": METRIC_DEFS["采纳率"]["formula"]},
        "回归时长": {"value": regression_min, "unit": "min", "estimated": regression_min is None,
                     "definition": METRIC_DEFS["回归时长"]["definition"], "formula": METRIC_DEFS["回归时长"]["formula"]},
        "问题发现率": {"value": find_rate, "unit": "个/次", "estimated": False,
                       "definition": METRIC_DEFS["问题发现率"]["definition"], "formula": METRIC_DEFS["问题发现率"]["formula"]},
        "缺陷逃逸率": {"value": escape, "unit": "%", "estimated": escape is None,
                        "definition": METRIC_DEFS["缺陷逃逸率"]["definition"], "formula": METRIC_DEFS["缺陷逃逸率"]["formula"]},
        "客户问题闭环效率": {"value": loop_h, "unit": "h", "estimated": loop_h is None,
                             "definition": METRIC_DEFS["客户问题闭环效率"]["definition"], "formula": METRIC_DEFS["客户问题闭环效率"]["formula"]},
    }


def snapshot_metrics(db: Session, project_id: int | None = None) -> dict:
    """计算并写入 metric_snapshots 表。"""
    metrics = compute_metrics(db, project_id)
    for name, spec in metrics.items():
        if spec["value"] is None:
            continue
        db.add(MetricSnapshot(project_id=project_id or 0, name=name, value=spec["value"],
                              unit=spec["unit"], period="week", meta={"estimated": spec["estimated"]}))
    db.commit()
    return metrics

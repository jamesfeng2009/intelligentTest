"""人工复核工作流（T47）：评审任务分配→复核→反馈回流。

- 任务/样本完成后生成 review_tasks（pending）
- 复核通过/驳回：feedback 回流，结果入库（影响评估数据：EvalSample.status → reviewed）
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from web.models import EvalSample, ReviewTask, Task


def create_review(db: Session, task_id: int | None = None, sample_id: int | None = None,
                  kind: str = "report", assignee: str = "qa") -> ReviewTask:
    """创建复核任务（幂等：同 task_id+sample_id+kind 已存在则返回既有）。"""
    q = db.query(ReviewTask).filter_by(kind=kind)
    if task_id:
        q = q.filter_by(task_id=task_id)
    if sample_id:
        q = q.filter_by(sample_id=sample_id)
    existing = q.first()
    if existing:
        return existing
    rt = ReviewTask(task_id=task_id, sample_id=sample_id, kind=kind, assignee=assignee)
    db.add(rt)
    db.commit()
    db.refresh(rt)
    return rt


def auto_create_reviews(db: Session) -> int:
    """为已完成的测试任务批量创建复核（无复核任务时）。"""
    created = 0
    tasks = db.query(Task).filter(Task.status == "done").all()
    for t in tasks:
        exists = db.query(ReviewTask).filter_by(task_id=t.id, kind="report").first()
        if not exists:
            db.add(ReviewTask(task_id=t.id, kind="report", assignee="qa"))
            created += 1
    # 已打分的样本创建复核（可复核评估数据）
    samples = db.query(EvalSample).filter(EvalSample.status == "graded").all()
    for s in samples:
        exists = db.query(ReviewTask).filter_by(sample_id=s.id, kind="sample").first()
        if not exists:
            db.add(ReviewTask(sample_id=s.id, kind="sample", assignee="qa"))
            created += 1
    if created:
        db.commit()
    return created


def submit_review(db: Session, review_id: int, status: str, feedback: str = "") -> dict:
    """提交复核结论：status approved/rejected；feedback 回流。"""
    rt = db.get(ReviewTask, review_id)
    if rt is None:
        return {"ok": False, "error": "复核任务不存在"}
    rt.status = "reviewed"
    rt.feedback = feedback
    rt.reviewed_at = datetime.now()
    # 样本复核：结果影响评估数据
    if rt.sample_id:
        sample = db.get(EvalSample, rt.sample_id)
        if sample is not None:
            sample.status = "reviewed" if status == "approved" else sample.status
            sample.graded = dict(sample.graded or {})
            sample.graded["review"] = {"status": status, "feedback": feedback}
    # 任务复核：写回任务
    if rt.task_id:
        task = db.get(Task, rt.task_id)
        if task is not None:
            task.review_status = status
            task.result = dict(task.result or {})
            task.result["review"] = {"status": status, "feedback": feedback}
    db.commit()
    return {"ok": True, "review_id": review_id, "status": status, "feedback": feedback}


def review_queue(db: Session, status: str | None = None) -> list[dict]:
    q = db.query(ReviewTask).order_by(ReviewTask.id.desc())
    if status:
        q = q.filter_by(status=status)
    rows = q.limit(100).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id, "kind": r.kind, "task_id": r.task_id, "sample_id": r.sample_id,
            "assignee": r.assignee, "status": r.status, "feedback": r.feedback,
            "created_at": r.created_at.isoformat(), "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        })
    return out

"""任务与报告路由（T33/T34/T38）：创建向导提交 / 列表 / 详情 / 报告。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import queue
from ..db import get_session
from ..models import Report, Task
from ..schemas import ReportOut, TaskIn, TaskOut
from .auth import require_role

router = APIRouter()


@router.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(body: TaskIn, db: Session = Depends(get_session),
                user: dict = Depends(require_role("task", "w"))):
    t = Task(
        project_id=body.project_id,
        repo_id=body.repo_id,
        title=body.title,
        task_type=body.task_type,
        requirement=body.requirement,
        scope=body.scope,
        status="pending",
        meta=body.meta,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    # 入队执行（返回后异步跑，前端轮询状态）
    queue.enqueue(t.id)
    return t


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(project_id: int | None = None, status_: str | None = None, limit: int = 50,
               db: Session = Depends(get_session), user: dict = Depends(require_role("task", "r"))):
    q = db.query(Task).order_by(Task.id.desc())
    if project_id:
        q = q.filter_by(project_id=project_id)
    if status_:
        q = q.filter_by(status=status_)
    return q.limit(limit).all()


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_session),
             user: dict = Depends(require_role("task", "r"))):
    t = db.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return t


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
def retry_task(task_id: int, db: Session = Depends(get_session),
               user: dict = Depends(require_role("task", "w"))):
    t = db.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    t.status = "pending"
    t.error = ""
    t.result = {}
    t.review_status = "none"
    db.commit()
    queue.enqueue(t.id)
    return t


@router.get("/tasks/{task_id}/reports", response_model=list[ReportOut])
def task_reports(task_id: int, db: Session = Depends(get_session),
                 user: dict = Depends(require_role("report", "r"))):
    return db.query(Report).filter_by(task_id=task_id).order_by(Report.id).all()


@router.get("/reports", response_model=list[ReportOut])
def list_reports(limit: int = 50, db: Session = Depends(get_session),
                 user: dict = Depends(require_role("report", "r"))):
    return db.query(Report).order_by(Report.id.desc()).limit(limit).all()


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int, db: Session = Depends(get_session),
               user: dict = Depends(require_role("report", "r"))):
    r = db.get(Report, report_id)
    if r is None:
        raise HTTPException(404, "报告不存在")
    return r

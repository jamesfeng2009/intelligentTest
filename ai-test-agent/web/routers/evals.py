"""评估体系路由（T44-T48）：数据集 / Grader / Trace / 人工复核 / 指标大盘。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import EvalSample
from ..schemas import ReviewIn
from .auth import require_role

router = APIRouter()


# ---------------- 数据集与 Grader ----------------
@router.post("/evals/datasets/seed")
def seed_datasets(db: Session = Depends(get_session), user: dict = Depends(require_role("eval", "w"))):
    from eval.datasets import dataset_stats, seed_datasets

    created = seed_datasets(db)
    return {"created": created, "stats": dataset_stats(db)}


@router.get("/evals/datasets/stats")
def datasets_stats(db: Session = Depends(get_session), user: dict = Depends(require_role("eval", "r"))):
    from eval.datasets import dataset_stats

    return dataset_stats(db)


@router.get("/evals/samples")
def list_samples(category: str | None = None, status: str | None = None, limit: int = 100,
                 db: Session = Depends(get_session), user: dict = Depends(require_role("eval", "r"))):
    q = db.query(EvalSample).order_by(EvalSample.id)
    if category:
        q = q.filter_by(category=category)
    if status:
        q = q.filter_by(status=status)
    rows = q.limit(limit).all()
    return [{"id": r.id, "category": r.category, "title": r.title, "prompt": r.prompt,
             "test_case": r.test_case, "labels": r.labels, "graded": r.graded, "status": r.status} for r in rows]


@router.post("/evals/grade")
def run_grader(body: dict | None = None, db: Session = Depends(get_session),
               user: dict = Depends(require_role("eval", "w"))):
    from eval.grader import run_grader

    body = body or {}
    return run_grader(db, limit=body.get("limit"), use_llm=bool(body.get("use_llm")))


# ---------------- 人工复核 ----------------
@router.get("/evals/reviews")
def list_reviews(status: str | None = None, db: Session = Depends(get_session),
                 user: dict = Depends(require_role("eval", "r"))):
    from eval.review import review_queue

    return review_queue(db, status)


@router.post("/evals/reviews/auto-create")
def auto_create(db: Session = Depends(get_session), user: dict = Depends(require_role("eval", "w"))):
    from eval.review import auto_create_reviews

    return {"created": auto_create_reviews(db)}


@router.post("/evals/reviews/{review_id}")
def submit_review(review_id: int, body: ReviewIn, db: Session = Depends(get_session),
                  user: dict = Depends(require_role("eval", "w"))):
    from eval.review import submit_review

    return submit_review(db, review_id, body.status, body.feedback)


# ---------------- Trace 可观测 ----------------
@router.get("/evals/traces")
def list_traces(limit: int = 20, db: Session = Depends(get_session),
                user: dict = Depends(require_role("eval", "r"))):
    from eval.trace import recent_traces

    return [{"trace_id": t} for t in recent_traces(db, limit)]


@router.get("/evals/traces/{trace_id}")
def get_trace(trace_id: str, db: Session = Depends(get_session),
              user: dict = Depends(require_role("eval", "r"))):
    from eval.trace import query_trace

    return query_trace(db, trace_id)


# ---------------- 工作路径聚合（Inspector 借鉴 C：SKILL/模板候选输入） ----------------
@router.get("/evals/paths")
def stable_paths(project_id: int | None = None, min_count: int = 2, limit: int = 20,
                 db: Session = Depends(get_session), user: dict = Depends(require_role("eval", "r"))):
    """稳定工作路径清单：按状态机序列+审批决议+失败分类聚类多次任务，
    输出 SKILL/模板沉淀的候选输入（min_count 默认 2 = 重复出现才算稳定）。"""
    from eval.path_extract import list_stable_paths

    return list_stable_paths(db, project_id=project_id, min_count=min_count, limit=limit)


# ---------------- 指标大盘 ----------------
@router.get("/metrics")
def get_metrics(project_id: int | None = None, db: Session = Depends(get_session),
                user: dict = Depends(require_role("metric", "r"))):
    from eval.metrics import compute_metrics

    return compute_metrics(db, project_id)


@router.post("/metrics/snapshot")
def snapshot(project_id: int | None = None, db: Session = Depends(get_session),
             user: dict = Depends(require_role("metric", "w"))):
    from eval.metrics import snapshot_metrics

    return snapshot_metrics(db, project_id)


@router.get("/metrics/history")
def metrics_history(project_id: int | None = None, limit: int = 50,
                    db: Session = Depends(get_session), user: dict = Depends(require_role("metric", "r"))):
    from ..models import MetricSnapshot

    q = db.query(MetricSnapshot).order_by(MetricSnapshot.id.desc())
    if project_id:
        q = q.filter_by(project_id=project_id)
    rows = q.limit(limit).all()
    return [{"name": r.name, "value": r.value, "unit": r.unit, "period": r.period,
             "created_at": r.created_at.isoformat()} for r in rows]

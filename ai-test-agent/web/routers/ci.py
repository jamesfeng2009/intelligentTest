"""CI/CD 路由（T49-T51）：Webhook 触发规则 / 触发执行 / 结果回流。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Repository, TriggerRule
from ..schemas import WebhookIn
from .auth import require_role

router = APIRouter()


# ---------------- Webhook 接收（无需写权限：外部系统回调） ----------------
@router.post("/ci/webhook")
def webhook(body: WebhookIn, db: Session = Depends(get_session)):
    from ci.webhook import dispatch_webhook

    return dispatch_webhook(db, body.event, body.repo, body.branch, body.payload)


# ---------------- 触发规则管理 ----------------
@router.get("/ci/rules")
def list_rules(db: Session = Depends(get_session), user: dict = Depends(require_role("ci", "r"))):
    rows = db.query(TriggerRule).order_by(TriggerRule.id).all()
    out = []
    for r in rows:
        repo = db.get(Repository, r.repo_id)
        out.append({"id": r.id, "repo_id": r.repo_id, "repo": repo.name if repo else "",
                    "trigger_type": r.trigger_type, "pattern": r.pattern,
                    "actions": r.actions, "enabled": r.enabled})
    return out


@router.post("/ci/rules", status_code=201)
def create_rule(body: dict, db: Session = Depends(get_session),
                user: dict = Depends(require_role("ci", "w"))):
    if db.get(Repository, body.get("repo_id")) is None:
        raise HTTPException(404, "仓库不存在")
    rule = TriggerRule(
        repo_id=body["repo_id"],
        trigger_type=body.get("trigger_type", "pr"),
        pattern=body.get("pattern", "*"),
        actions=body.get("actions", ["whitebox"]),
        enabled=body.get("enabled", True),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, "repo_id": rule.repo_id, "trigger_type": rule.trigger_type,
            "pattern": rule.pattern, "actions": rule.actions, "enabled": rule.enabled}


@router.delete("/ci/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_session),
                user: dict = Depends(require_role("ci", "w"))):
    rule = db.get(TriggerRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    db.delete(rule)
    db.commit()


# ---------------- 结果回流 ----------------
@router.get("/ci/feedback/{task_id}")
def feedback(task_id: int, db: Session = Depends(get_session),
             user: dict = Depends(require_role("ci", "r"))):
    from ci.feedback import feedback_message

    return feedback_message(db, task_id)


@router.get("/ci/feedback/{task_id}/pr-comment")
def pr_comment(task_id: int, db: Session = Depends(get_session),
               user: dict = Depends(require_role("ci", "r"))):
    from ci.feedback import build_pr_comment

    return {"content": build_pr_comment(db, task_id)}

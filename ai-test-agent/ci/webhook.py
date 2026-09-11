"""触发与 Webhook（T49）：PR 提交 / 合并 main / 每日定时 三种触发。

- 接收 webhook 事件（push / pull_request / schedule）
- 按 TriggerRule 匹配（repo + 事件 + 分支模式）→ 创建任务并进入队列
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from web.models import Repository, Task, TriggerRule
from web.queue import enqueue


def dispatch_webhook(db: Session, event: str, repo_name: str, branch: str,
                     payload: dict | None = None) -> dict:
    """处理一次 webhook：返回创建的 task 列表。"""
    payload = payload or {}
    repo = db.query(Repository).filter_by(name=repo_name).first()
    if repo is None:
        return {"ok": False, "error": f"仓库 {repo_name} 未注册，忽略事件", "tasks": []}

    # 事件归一：pull_request → pr；push+main → merge；schedule → cron
    evt = event.lower()
    if evt == "pull_request":
        evt_type = "pr"
    elif evt == "push":
        evt_type = "merge" if branch in (repo.default_branch, "main", "master") else "pr"
    else:
        evt_type = "cron"

    rules = db.query(TriggerRule).filter_by(repo_id=repo.id, enabled=True).all()
    matched = [r for r in rules if r.trigger_type == evt_type]
    if not matched:
        return {"ok": True, "created": 0, "tasks": [], "note": f"无 {evt_type} 触发规则"}

    created_tasks = []
    for rule in matched:
        actions = rule.actions or ["whitebox"]
        task_type = "orchestrator" if "orchestrator" in actions or len(actions) > 1 else actions[0]
        requirement = (
            f"Webhook 触发回归（{evt_type} @ {repo_name}/{branch}）："
            + "；".join(f"{a} 回归验证" for a in actions)
        )
        task = Task(
            project_id=repo.project_id,
            repo_id=repo.id,
            title=f"[{evt_type}] {repo_name} {branch} 触发回归",
            task_type=task_type,
            requirement=requirement,
            scope=actions,
            status="pending",
            meta={"trigger": {"event": event, "branch": branch, "repo": repo_name}, "repo_path": repo.local_path},
        )
        db.add(task)
        db.flush()
        created_tasks.append(task.id)
        enqueue(task.id)
    db.commit()
    return {"ok": True, "created": len(created_tasks), "tasks": created_tasks, "repo": repo_name, "branch": branch}

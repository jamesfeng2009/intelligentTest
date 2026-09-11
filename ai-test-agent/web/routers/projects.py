"""项目 / 仓库 / Agent 配置 / 系统路由（T32/T35/T36）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AgentConfig, Project, Repository
from ..schemas import ProjectIn, ProjectOut, RepositoryIn, RepositoryOut
from .auth import get_current_user, require_role, role_matrix

router = APIRouter()


# ---------------- 项目 ----------------
@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_session), user: dict = Depends(require_role("project", "r"))):
    return db.query(Project).order_by(Project.id.desc()).all()


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_session),
                   user: dict = Depends(require_role("project", "w"))):
    p = Project(name=body.name, description=body.description)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_session),
                user: dict = Depends(require_role("project", "r"))):
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    return p


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_session),
                   user: dict = Depends(require_role("project", "w"))):
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    db.delete(p)
    db.commit()


# ---------------- 仓库 ----------------
@router.get("/repos", response_model=list[RepositoryOut])
def list_repos(db: Session = Depends(get_session), user: dict = Depends(require_role("repo", "r"))):
    return db.query(Repository).order_by(Repository.id.desc()).all()


@router.post("/repos", response_model=RepositoryOut, status_code=201)
def create_repo(body: RepositoryIn, db: Session = Depends(get_session),
                user: dict = Depends(require_role("repo", "w"))):
    if db.get(Project, body.project_id) is None:
        raise HTTPException(404, "项目不存在")
    r = Repository(**body.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(repo_id: int, db: Session = Depends(get_session),
                user: dict = Depends(require_role("repo", "w"))):
    r = db.get(Repository, repo_id)
    if r is None:
        raise HTTPException(404, "仓库不存在")
    db.delete(r)
    db.commit()


# ---------------- Agent 配置 ----------------
@router.get("/agents")
def list_agents(db: Session = Depends(get_session), user: dict = Depends(require_role("agent", "r"))):
    rows = db.query(AgentConfig).order_by(AgentConfig.id).all()
    return [{"id": r.id, "name": r.name, "enabled": r.enabled, "model": r.model, "harness": r.harness} for r in rows]


@router.put("/agents/{agent_id}")
def update_agent(agent_id: int, body: dict, db: Session = Depends(get_session),
                 user: dict = Depends(require_role("agent", "w"))):
    a = db.get(AgentConfig, agent_id)
    if a is None:
        raise HTTPException(404, "Agent 不存在")
    if "enabled" in body:
        a.enabled = bool(body["enabled"])
    if "model" in body:
        a.model = str(body["model"])
    if "harness" in body and isinstance(body["harness"], dict):
        a.harness = body["harness"]
    db.commit()
    return {"id": a.id, "name": a.name, "enabled": a.enabled, "model": a.model, "harness": a.harness}


@router.post("/agents/seed")
def seed_agents(db: Session = Depends(get_session), user: dict = Depends(require_role("agent", "w"))):
    defaults = [
        ("orchestrator", {"max_steps": 8, "approvals": ["计划审批", "发布审批"], "guards": ["越权防护", "重试上限"]}),
        ("api", {"max_cases": 12, "guards": ["请求超时 10s", "状态码校验"]}),
        ("ui", {"max_steps": 20, "guards": ["定位超时 5s", "自愈重试 2"]}),
        ("whitebox", {"max_functions": 20, "guards": ["危险模式检查", "变更范围限制"]}),
        ("verifier", {"guards": ["结果交叉校验", "失败分类复核"]}),
    ]
    created = 0
    for name, harness in defaults:
        if not db.query(AgentConfig).filter_by(name=name).first():
            db.add(AgentConfig(name=name, enabled=True, model="mock", harness=harness))
            created += 1
    db.commit()
    return {"created": created}


# ---------------- 系统 / 权限矩阵 ----------------
@router.get("/system/roles")
def get_roles(user: dict = Depends(require_role("system", "r"))):
    return role_matrix()


@router.get("/system/info")
def system_info(user: dict = Depends(get_current_user)):
    from ..db import engine

    url = str(engine.url).split("@")[-1]
    return {"database": url, "mode": "sqlite" if url.startswith("sqlite") else "postgresql",
            "storage": "minio" if __import__("os").environ.get("MINIO_ENDPOINT") else "local"}

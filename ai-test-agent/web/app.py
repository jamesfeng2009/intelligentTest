"""FastAPI 应用入口（T36）：REST API + 静态托管 + 产物存储路由。

启动：
    cd ai-test-agent && python -m uvicorn web.app:app --port 8000
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import get_session, init_db
from .routers import auth, ci, evals, knowledge, projects, tasks
from .storage import create_storage

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="AI 测试智能体平台",
    description="多 Agent + Harness 的智能测试平台：任务编排 / 黑盒 API·UI / 白盒分析 / 知识库 RAG / 评估体系 / CI 集成",
    version="0.7.0",
)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    from . import queue

    queue.start_worker()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "ai-test-agent", "version": "0.7.0"}


# 认证
@app.post("/api/auth/login")
def login(body: dict):
    info = auth.authenticate(str(body.get("username", "")), str(body.get("password", "")))
    if info is None:
        raise HTTPException(401, "用户名或密码错误")
    return info


@app.get("/api/auth/me")
def me(user: dict = Depends(auth.get_current_user)):
    return user


# 业务路由
app.include_router(projects.router, prefix="/api", tags=["项目/仓库/Agent/系统"])
app.include_router(tasks.router, prefix="/api", tags=["任务/报告"])
app.include_router(knowledge.router, prefix="/api", tags=["知识库 RAG"])
app.include_router(evals.router, prefix="/api", tags=["评估体系"])
app.include_router(ci.router, prefix="/api", tags=["CI/CD"])


# 产物存储访问
_storage = create_storage()


@app.get("/storage/{key:path}")
def storage_get(key: str):
    data = _storage.get(key)
    if data is None:
        raise HTTPException(404, "产物不存在")
    ctype = "image/png" if key.endswith(".png") else ("text/markdown" if key.endswith(".md") else "application/octet-stream")
    return Response(content=data, media_type=ctype)


# 前端静态托管（12 页面 SPA）
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------- 项目 / 仓库 ----------------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RepositoryIn(BaseModel):
    project_id: int
    name: str
    url: str = ""
    local_path: str = ""
    default_branch: str = "main"
    language: str = "python"


class RepositoryOut(BaseModel):
    id: int
    project_id: int
    name: str
    url: str
    local_path: str
    default_branch: str
    language: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------- 任务 ----------------
class TaskIn(BaseModel):
    project_id: int
    repo_id: int | None = None
    title: str = Field(min_length=1, max_length=200)
    task_type: str = "orchestrator"  # api/ui/whitebox/orchestrator
    requirement: str = ""
    scope: list[str] = ["api", "ui", "whitebox"]
    meta: dict[str, Any] = Field(default_factory=dict)  # base_url/base_commit/target_commit/repo_path/ui_url


class TaskOut(BaseModel):
    id: int
    project_id: int
    repo_id: int | None
    title: str
    task_type: str
    requirement: str
    scope: list
    status: str
    review_status: str
    meta: dict
    result: dict
    error: str
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    id: int
    task_id: int
    report_type: str
    summary: str
    passed: int
    failed: int
    total: int
    passed_rate: float
    detail: dict
    artifact_paths: list
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------- 知识库 ----------------
class KnowledgeUploadIn(BaseModel):
    project_id: int
    doc_type: str  # prd/api/cases/defects/checklist
    name: str
    content: str


class KnowledgeDocOut(BaseModel):
    id: int
    project_id: int
    doc_type: str
    name: str
    status: str
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class RagQueryIn(BaseModel):
    project_id: int
    question: str
    top_k: int = 3
    doc_types: list[str] | None = None


# ---------------- 评估 ----------------
class ReviewIn(BaseModel):
    status: str  # approved/rejected
    feedback: str = ""


class MetricOut(BaseModel):
    id: int
    project_id: int
    name: str
    value: float
    unit: str
    period: str
    meta: dict
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------- CI ----------------
class WebhookIn(BaseModel):
    event: str  # push / pull_request / schedule
    repo: str = ""
    branch: str = "main"
    payload: dict[str, Any] = Field(default_factory=dict)

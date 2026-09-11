"""SQLAlchemy 模型（对齐任务清单 T37）。

用户 / 项目 / 仓库 / 任务 / 报告 / 知识库 / 评估 / 指标 / Agent 配置 / Webhook 规则。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.now()


# ---------------- 用户与权限（T35 权限矩阵） ----------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="viewer")  # admin/qa/developer/viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")


# ---------------- 项目 / 仓库 ----------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    owner: Mapped[User | None] = relationship(back_populates="projects")
    repositories: Mapped[list["Repository"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(512), default="")
    local_path: Mapped[str] = mapped_column(String(512), default="")  # 本地白盒分析路径
    default_branch: Mapped[str] = mapped_column(String(64), default="main")
    language: Mapped[str] = mapped_column(String(32), default="python")  # python/javascript/typescript/go
    status: Mapped[str] = mapped_column(String(32), default="active")  # active/archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    project: Mapped[Project] = relationship(back_populates="repositories")
    tasks: Mapped[list["Task"]] = relationship(back_populates="repo")


# ---------------- 任务（T38） ----------------
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    repo_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    task_type: Mapped[str] = mapped_column(String(32), index=True)  # api/ui/whitebox/orchestrator
    requirement: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[list] = mapped_column(JSON, default=list)  # ["api","ui","whitebox"]
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending → running → done / failed；done 后可进 review（人工复核）
    review_status: Mapped[str] = mapped_column(String(32), default="none")  # none/pending/approved/rejected
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # base_url/commits/repo_path/trigger...
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # 汇总结果（summary/passed/failed/...）
    error: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="tasks")
    repo: Mapped[Repository | None] = relationship(back_populates="tasks")
    reports: Mapped[list["Report"]] = relationship(back_populates="task", cascade="all, delete-orphan")


# ---------------- 报告（T34） ----------------
class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    report_type: Mapped[str] = mapped_column(String(32))  # api/ui/whitebox/orchestrator
    summary: Mapped[str] = mapped_column(Text, default="")
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed_rate: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # 完整结果（用例/变更函数/走查意见）
    artifact_paths: Mapped[list] = mapped_column(JSON, default=list)  # 报告/截图等产物链接
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    task: Mapped[Task] = relationship(back_populates="reports")


# ---------------- 知识库 RAG（T40-T43） ----------------
class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(32), index=True)  # prd/api/cases/defects/checklist
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="processing")  # processing/ready/failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(back_populates="doc", cascade="all, delete-orphan")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("knowledge_docs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    vector: Mapped[list] = mapped_column(JSON, default=list)  # embedding 向量（本地 TF-IDF 或模型向量）
    source: Mapped[str] = mapped_column(String(256), default="")
    doc_type: Mapped[str] = mapped_column(String(32), index=True, default="")

    doc: Mapped[KnowledgeDoc] = relationship(back_populates="chunks")


# ---------------- 评估体系（T44-T48） ----------------
class EvalSample(Base):
    __tablename__ = "eval_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)  # api_case/ui_script/whitebox_case
    title: Mapped[str] = mapped_column(String(200), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    test_case: Mapped[str] = mapped_column(Text, default="")  # 模型生成的用例
    labels: Mapped[dict] = mapped_column(JSON, default=dict)  # 人工标注：coverage/correctness/stability
    graded: Mapped[dict] = mapped_column(JSON, default=dict)  # Grader 打分结果
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/graded/reviewed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    sample_id: Mapped[int | None] = mapped_column(ForeignKey("eval_samples.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="report")  # report/sample
    assignee: Mapped[str] = mapped_column(String(64), default="qa")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/reviewed
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TraceEntry(Base):
    __tablename__ = "trace_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    step: Mapped[str] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(16), default="info")  # info/warning/error
    input: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)  # 覆盖率/采纳率/回归时长/问题发现率/缺陷逃逸率/闭环效率
    value: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), default="%")
    period: Mapped[str] = mapped_column(String(32), default="week")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---------------- Agent 配置（T35）与 Webhook（T49） ----------------
class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # orchestrator/api/ui/whitebox/verifier
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    harness: Mapped[dict] = mapped_column(JSON, default=dict)  # max_steps/approvals/guards...
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TriggerRule(Base):
    __tablename__ = "trigger_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    trigger_type: Mapped[str] = mapped_column(String(32))  # pr/merge/cron
    pattern: Mapped[str] = mapped_column(String(256), default="*")  # 分支/路径/时间
    actions: Mapped[list] = mapped_column(JSON, default=list)  # ["whitebox","api"]
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

"""数据库与会话管理。

开发默认 SQLite（零依赖、开箱即演示）；生产通过 DATABASE_URL 切换到 PostgreSQL。
对齐任务清单 T37：用户/项目/仓库/任务/报告/指标等模型。
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "app.db"


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    """优先读 DATABASE_URL（如 postgresql+psycopg://user:pwd@localhost:5432/ai_test），否则 SQLite。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{_DEFAULT_DB}"


engine = create_engine(
    _database_url(),
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _database_url().startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(engine)
    _migrate(engine)


def _migrate(engine) -> None:
    """轻量迁移：为已存在的表补充新列（SQLite 无原生 ALTER 语义，幂等处理）。"""
    import sqlalchemy as sa

    try:
        insp = sa.inspect(engine)
        if not insp.has_table("knowledge_chunks"):
            return
        cols = {c["name"] for c in insp.get_columns("knowledge_chunks")}
        with engine.begin() as conn:
            if "parent_id" not in cols:
                conn.execute(sa.text("ALTER TABLE knowledge_chunks ADD COLUMN parent_id INTEGER DEFAULT 0"))
            if "is_parent" not in cols:
                conn.execute(sa.text("ALTER TABLE knowledge_chunks ADD COLUMN is_parent BOOLEAN DEFAULT 0"))
    except Exception:  # noqa: BLE001 迁移失败不阻断启动（新库 create_all 已含新列）
        pass


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

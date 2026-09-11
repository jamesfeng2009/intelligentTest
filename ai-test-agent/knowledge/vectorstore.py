"""向量存储抽象层（P2）：统一检索入口，支持多后端，缺依赖自动降级。

- DbVectorStore（默认）：子块向量存 SQLite JSON 列（现状，零依赖）
- ChromaVectorStore（可选）：本地 Chroma 持久化（真向量库，pip install chromadb）
- PgVectorStore（生产可选）：pgvector，需 DATABASE_URL 指向 PostgreSQL

设计：向量索引只存 chunk_id + vector + 元数据；DB 行始终是内容权威源
（父块全文 / 来源 / 文档归属仍从 DB 回查）。切换后端后可用 rebuild_index 回填。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class VectorHit:
    chunk_id: int
    score: float


class VectorStore(Protocol):
    name: str

    def query(self, vector: list[float], top_k: int, project_id: int,
              doc_types: list[str] | None = None) -> list[VectorHit]: ...

    def upsert(self, chunk_id: int, vector: list[float], metadata: dict) -> None: ...

    def rebuild(self, db) -> None: ...


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class DbVectorStore:
    """默认后端：子块向量存 KnowledgeChunk.vector（JSON 列），内存余弦检索。"""

    name = "db"

    def __init__(self, db) -> None:
        self._db = db

    def query(self, vector: list[float], top_k: int, project_id: int,
              doc_types: list[str] | None = None) -> list[VectorHit]:
        from sqlalchemy import select

        from web.models import KnowledgeChunk, KnowledgeDoc

        stmt = select(KnowledgeChunk.id, KnowledgeChunk.vector).join(
            KnowledgeDoc, KnowledgeDoc.id == KnowledgeChunk.doc_id
        ).where(
            KnowledgeDoc.project_id == project_id,
            KnowledgeChunk.is_parent.is_(False),
        )
        if doc_types:
            stmt = stmt.where(KnowledgeChunk.doc_type.in_(doc_types))
        scored = []
        for cid, vec in self._db.execute(stmt).all():
            score = _cosine(vector, vec or [])
            if score > 0.05:
                scored.append(VectorHit(cid, score))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    def upsert(self, chunk_id: int, vector: list[float], metadata: dict) -> None:
        from web.models import KnowledgeChunk

        row = self._db.get(KnowledgeChunk, chunk_id)
        if row:
            row.vector = vector
            self._db.commit()

    def rebuild(self, db) -> None:
        pass  # 默认后端即 DB 自身，无需重建


class ChromaVectorStore:
    """Chroma 后端：本地持久化（data/chroma）。元数据过滤对齐项目/文档类型。"""

    name = "chroma"

    def __init__(self, db, persist_dir: str | None = None) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=persist_dir or self._default_dir())
        self._collection = self._client.get_or_create_collection(
            "knowledge_chunks", metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _default_dir() -> str:
        from pathlib import Path

        env = os.environ.get("AI_TEST_VECTOR_STORE_DIR")
        if env:
            return env
        return str(Path(__file__).parent.parent / "data" / "chroma")

    def query(self, vector: list[float], top_k: int, project_id: int,
              doc_types: list[str] | None = None) -> list[VectorHit]:
        where = {"project_id": project_id}
        if doc_types:
            where["doc_type"] = {"$in": doc_types}
        try:
            res = self._collection.query(
                query_embeddings=[vector], n_results=top_k, where=where,
                include=["distances"],
            )
        except Exception:  # noqa: BLE001 集合为空/过滤无命中
            return []
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out = []
        for cid, d in zip(ids, dists):
            try:
                out.append(VectorHit(int(cid), round(1.0 - float(d), 4)))
            except (TypeError, ValueError):
                continue
        return out

    def upsert(self, chunk_id: int, vector: list[float], metadata: dict) -> None:
        meta = {k: v for k, v in metadata.items() if v is not None}
        self._collection.upsert(ids=[str(chunk_id)], embeddings=[vector], metadatas=[meta])

    def rebuild(self, db) -> None:
        """从 DB 回填全量子块向量（切换后端后调用一次）。"""
        from sqlalchemy import select

        from web.models import KnowledgeChunk, KnowledgeDoc

        rows = db.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.vector, KnowledgeChunk.doc_type,
                   KnowledgeDoc.project_id)
            .join(KnowledgeDoc, KnowledgeDoc.id == KnowledgeChunk.doc_id)
            .where(KnowledgeChunk.is_parent.is_(False))
        ).all()
        ids, vecs, metas = [], [], []
        for cid, vec, dtype, pid in rows:
            if vec:
                ids.append(str(cid)); vecs.append(vec)
                metas.append({"project_id": pid, "doc_type": dtype or ""})
        if ids:
            self._collection.upsert(ids=ids, embeddings=vecs, metadatas=metas)


class PgVectorStore:
    """pgvector 后端（生产）：需 DATABASE_URL 指向 PostgreSQL 且启用 pgvector 扩展。

    未在本地环境验证（无 PG 实例）；接口与 Chroma 一致，接入时创建
    vector_entries(chunk_id bigint PK, vector vector, project_id int, doc_type text) 表即可。
    """

    name = "pgvector"

    def __init__(self, db) -> None:
        self._db = db
        db.execute("SELECT 1")  # 触发连接校验，失败即抛错（上层捕获降级）

    def query(self, vector: list[float], top_k: int, project_id: int,
              doc_types: list[str] | None = None) -> list[VectorHit]:
        raise NotImplementedError("PgVectorStore 需生产 PostgreSQL 环境验证")

    def upsert(self, chunk_id: int, vector: list[float], metadata: dict) -> None:
        raise NotImplementedError("PgVectorStore 需生产 PostgreSQL 环境验证")

    def rebuild(self, db) -> None:
        raise NotImplementedError("PgVectorStore 需生产 PostgreSQL 环境验证")


def create_vectorstore(db) -> VectorStore:
    """按 AI_TEST_VECTOR_STORE 选型（auto|db|chroma|pgvector），缺依赖自动降级 db。"""
    choice = os.environ.get("AI_TEST_VECTOR_STORE", "auto").lower()
    if choice in ("chroma", "auto"):
        try:
            return ChromaVectorStore(db)
        except Exception as e:  # noqa: BLE001
            if choice == "chroma":
                print(f"[vectorstore] chroma 不可用，降级 db：{e}")
    if choice in ("pgvector",):
        try:
            return PgVectorStore(db)
        except Exception as e:  # noqa: BLE001
            print(f"[vectorstore] pgvector 不可用，降级 db：{e}")
    return DbVectorStore(db)

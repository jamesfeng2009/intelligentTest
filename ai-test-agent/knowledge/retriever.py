"""向量检索 + RAG（T41/T42）。

- vector_store：基于 knowledge_chunks 表的余弦召回（TF-IDF 向量直接落库，可复算）
- retriever：按 doc_type 过滤 + top_k 召回，返回带来源引用的分片
- rag_build_context：把召回内容组装为 LLM 上下文（供用例生成等 Agent 注入）
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunking import tokenize


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def retrieve(db: Session, project_id: int, question: str, top_k: int = 3,
             doc_types: list[str] | None = None, embedder=None) -> list[dict]:
    """召回：query 向量 vs 全库分片向量，返回 [{chunk_id, content, score, source, doc_type}]。"""
    from knowledge.chunking import tokenize
    from knowledge.embeddings import TfidfEmbedder, create_embedder

    embedder = embedder or create_embedder()
    if isinstance(embedder, TfidfEmbedder) and not embedder._terms:
        # 本地 TF-IDF：词表未构建时从全库重建（幂等、可复算）
        all_texts = db.query(KnowledgeChunk.content).all()
        embedder.fit([tokenize(c[0]) for c in all_texts])
    stmt = select(KnowledgeChunk.id, KnowledgeChunk.content, KnowledgeChunk.source,
                  KnowledgeChunk.doc_type, KnowledgeChunk.vector).where(
        KnowledgeChunk.doc_id.in_(
            select(KnowledgeDoc.id).where(KnowledgeDoc.project_id == project_id)
        )
    )
    if doc_types:
        stmt = stmt.where(KnowledgeChunk.doc_type.in_(doc_types))
    rows = db.execute(stmt).all()
    if not rows:
        return []

    # 实时向量化（与 query 同一词表，避免入库词表漂移导致维度不一致）
    qvec = embedder.embed([question])[0]
    contents = [r[1] for r in rows]
    vecs = embedder.embed(contents)
    scored = []
    for (chunk_id, content, source, dtype, _vector), vec in zip(rows, vecs):
        score = _cosine(qvec, vec)
        if score > 0.05:  # 相关度阈值：过滤无关召回（支持"无相关上下文"判定）
            scored.append({"chunk_id": chunk_id, "content": content, "score": round(score, 4),
                           "source": source or dtype, "doc_type": dtype})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def build_rag_context(retrieved: list[dict]) -> str:
    """把召回分片组装成可注入的上下文文本（带来源引用，可追溯）。"""
    if not retrieved:
        return ""
    lines = ["【知识库召回（按相关度排序，来源可追溯）】"]
    for i, r in enumerate(retrieved, 1):
        lines.append(f"[{i}] 来源：{r.get('doc_type')}/{r.get('source')}（相关度 {r.get('score', 0):.3f}）")
        lines.append(r["content"].strip()[:400])
    return "\n".join(lines)


# 延迟导入避免循环依赖
from web.models import KnowledgeChunk, KnowledgeDoc  # noqa: E402

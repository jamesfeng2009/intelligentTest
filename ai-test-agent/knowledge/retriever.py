"""向量检索 + RAG（T41/T42）。

- vector_store：基于 knowledge_chunks 表的余弦召回（TF-IDF 向量直接落库，可复算）
- retriever：按 doc_type 过滤 + top_k 召回，返回带来源引用的分片
- rag_build_context：把召回内容组装为 LLM 上下文（供用例生成等 Agent 注入）
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunking import tokenize


def retrieve(db: Session, project_id: int, question: str, top_k: int = 3,
             doc_types: list[str] | None = None, embedder=None) -> list[dict]:
    """召回（P2）：query 向量 → vectorstore（可插拔后端）命中子块 → 回 DB 取子块 + 父块全文。

    返回 [{chunk_id, content, parent_content, score, source, doc_type}]：
    - content：命中的子块内容
    - parent_content：所属父块全文（父子拆分下优先作为 LLM 上下文）
    """
    from knowledge.chunking import tokenize
    from knowledge.embeddings import TfidfEmbedder, create_embedder
    from knowledge.vectorstore import create_vectorstore
    from sqlalchemy import select

    from web.models import KnowledgeChunk, KnowledgeDoc

    embedder = embedder or create_embedder()
    if isinstance(embedder, TfidfEmbedder) and not embedder._terms:
        # 本地 TF-IDF：词表未构建时从全库子块重建（幂等、可复算）
        all_texts = db.query(KnowledgeChunk.content).filter(KnowledgeChunk.is_parent.is_(False)).all()
        embedder.fit([tokenize(c[0]) for c in all_texts])

    qvec = embedder.embed([question])[0]
    store = create_vectorstore(db)
    hits = store.query(qvec, top_k=top_k * 2, project_id=project_id, doc_types=doc_types)
    if not hits:
        return []
    hit_map = {h.chunk_id: h.score for h in hits}
    rows = db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.content, KnowledgeChunk.source,
               KnowledgeChunk.doc_type, KnowledgeChunk.parent_id)
        .where(KnowledgeChunk.id.in_(list(hit_map.keys())))
    ).all()
    parent_ids = {r[4] for r in rows if r[4] and r[4] > 0}
    parent_map: dict[int, str] = {}
    if parent_ids:
        for p in db.query(KnowledgeChunk.id, KnowledgeChunk.content).filter(KnowledgeChunk.id.in_(parent_ids)).all():
            parent_map[p[0]] = p[1]
    scored = []
    for cid, content, source, dtype, pid in rows:
        score = hit_map[cid]
        if score <= 0.05:      # 相关度阈值统一过滤（db/chroma 后端一致）
            continue
        scored.append({"chunk_id": cid, "content": content,
                       "parent_content": parent_map.get(pid or 0, ""),
                       "score": round(score, 4),
                       "source": source or dtype, "doc_type": dtype})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def build_rag_context(retrieved: list[dict]) -> str:
    """把召回分片组装成可注入的上下文文本（父子结构下优先父块全文，来源可追溯）。"""
    if not retrieved:
        return ""
    lines = ["【知识库召回（按相关度排序，来源可追溯）】"]
    for i, r in enumerate(retrieved, 1):
        lines.append(f"[{i}] 来源：{r.get('doc_type')}/{r.get('source')}（相关度 {r.get('score', 0):.3f}）")
        body = (r.get("parent_content") or r["content"]).strip()
        lines.append(body[:600])
    return "\n".join(lines)


# 延迟导入避免循环依赖
from web.models import KnowledgeChunk, KnowledgeDoc  # noqa: E402

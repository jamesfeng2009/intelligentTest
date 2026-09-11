"""知识库 RAG 路由（T40-T43）：上传解析分片 / 列表 / 检索 / RAG 生成 / 召回评估。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import KnowledgeChunk, KnowledgeDoc
from ..schemas import KnowledgeDocOut, KnowledgeUploadIn, RagQueryIn
from .auth import require_role

router = APIRouter()


@router.post("/knowledge/upload", response_model=KnowledgeDocOut, status_code=201)
def upload_doc(body: KnowledgeUploadIn, db: Session = Depends(get_session),
               user: dict = Depends(require_role("knowledge", "w"))):
    from knowledge.chunking import DOC_TYPES, split_document_structured
    from knowledge.embeddings import TfidfEmbedder, create_embedder
    from knowledge.chunking import tokenize

    if body.doc_type not in DOC_TYPES:
        raise HTTPException(422, f"doc_type 必须是 {list(DOC_TYPES)}")
    doc = KnowledgeDoc(project_id=body.project_id, doc_type=body.doc_type, name=body.name, status="processing")
    db.add(doc)
    db.flush()

    # P1：父子拆分 —— 父块存全文（is_parent=True，不建向量），子块建向量并指向父块
    parents = split_document_structured(body.content, body.doc_type)
    child_texts = [c["content"] for p in parents for c in p["children"]]
    embedder = create_embedder()
    if isinstance(embedder, TfidfEmbedder):
        all_rows = db.query(KnowledgeChunk.content).filter(KnowledgeChunk.is_parent.is_(False)).all()
        embedder.fit([tokenize(c[0]) for c in all_rows] + [tokenize(c) for c in child_texts])
        vectors = embedder.embed(child_texts)
    else:
        vectors = embedder.embed(child_texts)

    n_child = 0
    store = None
    if vectors:
        from knowledge.vectorstore import create_vectorstore

        store = create_vectorstore(db)
    for pi, p in enumerate(parents):
        parent = KnowledgeChunk(doc_id=doc.id, seq=pi, content=p["parent_content"],
                                vector=[], is_parent=True,
                                source=f"{body.name}#父{pi + 1}", doc_type=body.doc_type)
        db.add(parent)
        db.flush()
        for c in p["children"]:
            vec = vectors[n_child]
            n_child += 1
            child = KnowledgeChunk(doc_id=doc.id, seq=c["seq"], content=c["content"], vector=vec,
                                   parent_id=parent.id,
                                   source=f"{body.name}#{c['seq'] + 1}", doc_type=body.doc_type)
            db.add(child)
            if store is not None and store.name != "db":
                db.flush()
                store.upsert(child.id, vec, {"project_id": body.project_id, "doc_type": body.doc_type})
    doc.status = "ready"
    doc.chunk_count = n_child
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/knowledge", response_model=list[KnowledgeDocOut])
def list_docs(project_id: int | None = None, db: Session = Depends(get_session),
              user: dict = Depends(require_role("knowledge", "r"))):
    q = db.query(KnowledgeDoc).order_by(KnowledgeDoc.id.desc())
    if project_id:
        q = q.filter_by(project_id=project_id)
    return q.all()


@router.get("/knowledge/{doc_id}/chunks")
def doc_chunks(doc_id: int, db: Session = Depends(get_session),
               user: dict = Depends(require_role("knowledge", "r"))):
    rows = db.query(KnowledgeChunk).filter_by(doc_id=doc_id).order_by(KnowledgeChunk.seq).all()
    return [{"seq": r.seq, "content": r.content[:500], "source": r.source} for r in rows]


@router.post("/knowledge/retrieve")
def retrieve(body: RagQueryIn, db: Session = Depends(get_session),
             user: dict = Depends(require_role("knowledge", "r"))):
    from knowledge.retriever import retrieve

    return retrieve(db, body.project_id, body.question, top_k=body.top_k, doc_types=body.doc_types)


@router.post("/knowledge/rag")
def rag_generate(body: RagQueryIn, db: Session = Depends(get_session),
                 user: dict = Depends(require_role("knowledge", "r"))):
    from core.llm import create_llm
    from knowledge.rag import rag_generate

    llm = create_llm()
    return rag_generate(db, body.project_id, body.question, llm.chat_text,
                        doc_types=body.doc_types, top_k=body.top_k)


@router.post("/knowledge/eval-recall")
def eval_recall(body: dict, db: Session = Depends(get_session),
                user: dict = Depends(require_role("knowledge", "w"))):
    """召回质量评估：body = {project_id, gold: {query: {gold_ids:[], answer:''}}}。"""
    from knowledge.eval_recall import evaluate_recall
    from knowledge.retriever import retrieve

    project_id = body.get("project_id")
    gold = body.get("gold", {})
    k = int(body.get("k", 3))

    def _retrieve(query: str, kk: int):
        return retrieve(db, project_id, query, top_k=kk)

    return evaluate_recall(gold, _retrieve, k=k)

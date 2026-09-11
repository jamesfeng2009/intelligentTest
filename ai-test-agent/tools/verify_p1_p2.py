"""P1/P2 综合验证：需求三级解析 / 父子拆分 / 检索（db + chroma 后端）/ 追溯矩阵。

用法：AI_TEST_VECTOR_STORE=auto python3 tools/verify_p1_p2.py
使用独立临时数据库，不污染历史 data/app.db。
"""
from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMPDIR = tempfile.mkdtemp(prefix="verify_p1p2_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/verify.db"
os.environ["AI_TEST_VECTOR_STORE_DIR"] = f"{_TMPDIR}/chroma"

from web.db import SessionLocal, init_db  # noqa: E402

init_db()

REQUIREMENT = """# 电商平台需求 v1.2

## 3. 用户中心
### 3.1 用户注册
新用户可注册：用户名密码不能为空，用户名唯一。
### 3.2 用户登录
用户凭用户名密码登录，登录成功返回 token；登录失败不得发放凭证。

## 4. 商品模块
### 4.1 商品发布
创建商品需填写名称与价格，价格必须大于 0，名称长度不超过 100；成功返回 201 与商品信息。
### 4.2 商品查询
按 id 查询商品，存在返回 200 与详情，不存在返回 404。

## 5. 权限模型
未携带有效凭证访问受保护接口应返回 401/403，不得绕过。
"""

ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/register"},
    {"method": "POST", "path": "/api/v1/login"},
    {"method": "POST", "path": "/api/v1/products"},
    {"method": "GET", "path": "/api/v1/products/{id}"},
    {"method": "PUT", "path": "/api/v1/products/{id}"},
]
PROJECT_ID = 999901


def seed_knowledge(db, embedder_holder: dict) -> None:
    """模拟 /knowledge/upload 的入库逻辑（父子拆分 + 向量化 + 可选的 chroma 镜像）。"""
    from knowledge.chunking import split_document_structured, tokenize
    from knowledge.embeddings import TfidfEmbedder, create_embedder
    from knowledge.vectorstore import create_vectorstore
    from web.models import KnowledgeChunk, KnowledgeDoc

    doc = KnowledgeDoc(project_id=PROJECT_ID, doc_type="prd", name="电商PRD", status="processing")
    db.add(doc)
    db.flush()
    parents = split_document_structured(REQUIREMENT)
    child_texts = [c["content"] for p in parents for c in p["children"]]
    embedder = create_embedder()
    if isinstance(embedder, TfidfEmbedder):
        embedder.fit([tokenize(t) for t in child_texts])
    vectors = embedder.embed(child_texts)
    embedder_holder["embedder"] = embedder
    store = create_vectorstore(db)
    n = 0
    for pi, p in enumerate(parents):
        parent = KnowledgeChunk(doc_id=doc.id, seq=pi, content=p["parent_content"],
                                vector=[], is_parent=True, source=f"电商PRD#父{pi + 1}", doc_type="prd")
        db.add(parent)
        db.flush()
        for c in p["children"]:
            child = KnowledgeChunk(doc_id=doc.id, seq=c["seq"], content=c["content"], vector=vectors[n],
                                   parent_id=parent.id, source=f"电商PRD#{c['seq'] + 1}", doc_type="prd")
            db.add(child)
            if store.name != "db":
                db.flush()
                store.upsert(child.id, vectors[n], {"project_id": PROJECT_ID, "doc_type": "prd"})
            n += 1
    doc.status = "ready"
    doc.chunk_count = n
    db.commit()
    return store


def main() -> None:
    from core.llm import MockLLM
    from knowledge.requirement_parser import (build_traceability, parse_requirement,
                                              render_requirement_md, render_traceability_md)

    # ---- 1. 需求三级解析（L1 结构 + L2 条目 + L3 要素，mock 兜底） ----
    llm = MockLLM()
    res = parse_requirement(REQUIREMENT, llm, endpoints=ENDPOINTS)
    assert res.items, "需求解析不应为空"
    assert res.sections, "L1 结构解析不应为空"
    print(f"[P1a] 需求条目 {len(res.items)} 个；章节 {len(res.sections)} 个")
    for it in res.items:
        eps = ",".join(f"{e['method']} {e['path']}" for e in it.involved_endpoints) or "-"
        print(f"      {it.id} {it.title} [{it.priority}] 接口={eps} 验收={len(it.acceptance)}")
    # 接口引用校验：无效端点应被过滤
    for it in res.items:
        for e in it.involved_endpoints:
            assert (e["method"], e["path"]) in [(x["method"], x["path"]) for x in ENDPOINTS], "引用了不存在的接口"
    print("      ✅ 接口引用校验通过（只保留真实端点）")

    # ---- 2. 父子拆分 ----
    from knowledge.chunking import split_document_structured
    parents = split_document_structured(REQUIREMENT)
    assert parents and all(p["children"] for p in parents), "父子拆分不应为空"
    clens = [len(c["content"]) for p in parents for c in p["children"]]
    plens = [len(p["parent_content"]) for p in parents]
    print(f"[P1c] 父块 {len(parents)}（长度 {min(plens)}~{max(plens)}，上限 1500）；"
          f"子块 {len(clens)}（长度 {min(clens)}~{max(clens)}，区间 150~400）")

    # ---- 3. 入库 + 检索（默认 db 后端） ----
    holder: dict = {}
    with SessionLocal() as db:
        seed_knowledge(db, holder)
        from knowledge.retriever import retrieve, build_rag_context
        hits = retrieve(db, PROJECT_ID, "用户登录失败场景与凭证发放", top_k=2,
                        embedder=holder["embedder"])
        assert hits, "db 后端检索不应为空"
        for h in hits:
            print(f"[P2-db] 命中 {h['source']} 相关度 {h['score']} 子块{len(h['content'])}字符 父块{len(h['parent_content'])}字符")
        assert all(h.get("parent_content") for h in hits), "父子检索应返回父块全文"
        ctx = build_rag_context(hits)
        assert "【知识库召回" in ctx
        print("      ✅ 命中子块均返回父块全文，RAG 上下文可注入")

    # ---- 4. Chroma 后端 ----
    os.environ["AI_TEST_VECTOR_STORE"] = "chroma"
    with SessionLocal() as db:
        from knowledge.vectorstore import create_vectorstore
        store = create_vectorstore(db)
        assert store.name == "chroma", f"期望 chroma，实际 {store.name}"
        store.rebuild(db)   # 从 DB 回填（该库在 db 后端时入库未镜像）
        from knowledge.retriever import retrieve
        hits = retrieve(db, PROJECT_ID, "商品发布的价格校验规则", top_k=2,
                        embedder=holder["embedder"])
        assert hits, "chroma 后端检索不应为空"
        for h in hits:
            print(f"[P2-chroma] 命中 {h['source']} 相关度 {h['score']} 父块{len(h['parent_content'])}字符")
    print("      ✅ Chroma 真实向量库检索可用")

    # ---- 5. 追溯矩阵（需求条目 ↔ 功能用例） ----
    cases = [
        {"id": "FC-001", "feature": "用户登录", "traceability": {"requirement": "用户登录"}},
        {"id": "FC-002", "feature": "用户登录-失败", "traceability": {"requirement": "用户登录"}},
        {"id": "FC-003", "feature": "商品创建", "traceability": {"requirement": "商品创建"}},
        {"id": "FC-004", "feature": "商品查询", "traceability": {"requirement": "商品查询"}},
    ]
    rows = build_traceability(res.items, cases)
    covered = [r for r in rows if r["status"] == "covered"]
    gaps = [r for r in rows if r["status"] == "gap"]
    print(f"[P1b] 追溯矩阵：{len(rows)} 条目，覆盖 {len(covered)}，缺口 {len(gaps)}")
    for r in rows:
        print(f"      {r['item_id']} {r['item_title']}: {r['status']} {r['covered_cases']}")
    md = render_traceability_md(rows)
    assert "追溯矩阵" in md
    print("      ✅ 追溯矩阵渲染通过")

    # ---- 6. 需求解析产物渲染 ----
    md2 = render_requirement_md(res)
    assert "需求解析" in md2
    print("[P1a] 需求解析 md 渲染通过")
    print("\n=== ALL P1/P2 VERIFY PASSED ===")


if __name__ == "__main__":
    main()

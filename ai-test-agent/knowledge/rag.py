"""RAG 增强生成（T42）：用例/测试方案生成时注入知识库上下文，减少幻觉。

- rag_generate：查知识库 → 组装上下文 → 调用 LLM 生成（mock 模式下基于召回拼接确定性输出）
- 生成结果携带 references 字段（来源引用），可追溯
"""
from __future__ import annotations

import json
from typing import Callable

from sqlalchemy.orm import Session

from .retriever import build_rag_context, retrieve


def rag_generate(db: Session, project_id: int, question: str, llm: Callable[[str, str], str],
                 doc_types: list[str] | None = None, top_k: int = 3,
                 prompt_builder: Callable[[str, str], str] | None = None) -> dict:
    """知识库增强生成。

    llm(system, user) -> str：与 core.llm 的 chat_text 兼容。
    prompt_builder(question, context) -> user_prompt：可自定义注入模板。
    """
    hits = retrieve(db, project_id, question, top_k=top_k, doc_types=doc_types)
    context = build_rag_context(hits)

    if prompt_builder is None:
        def _default(question_: str, context_: str) -> str:
            return (f"请基于以下知识库上下文回答测试问题（没有相关上下文时明确说明）：\n\n"
                    f"问题：{question_}\n\n{context_ or '（无相关召回）'}")

        prompt_builder = _default

    system = "你是 AI 测试智能体，回答必须只依据给定上下文，禁止编造；引用需带来源。"
    answer = llm(system, prompt_builder(question, context))

    return {
        "question": question,
        "answer": answer,
        "references": [{"source": r["source"], "doc_type": r["doc_type"], "score": r["score"]} for r in hits],
        "hit_count": len(hits),
    }

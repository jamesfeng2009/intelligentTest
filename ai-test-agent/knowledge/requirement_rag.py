"""需求侧 / 用例侧 RAG（P2 / B12）：相似需求、历史缺陷、操作手册、基线用例、上线检查清单检索注入。

- 需求侧（build_requirement_context）：相似需求复用 + 缺陷关联，注入需求解析与计划生成
- 用例侧（build_case_context）：操作手册(prd) + 基线用例(cases) + 上线检查清单(checklist)
  注入功能 / API / UI 用例生成 —— 课程 2.8 的核心闭环
- 知识库为空或不可用时静默返回空串（不阻断主流程）
"""
from __future__ import annotations

from .retriever import build_rag_context, retrieve


def build_requirement_context(db, project_id: int, requirement_text: str) -> str:
    """组装注入需求解析/测试计划生成的 RAG 上下文（few-shot 复用 + 缺陷关联）。"""
    parts: list[str] = []
    similar = retrieve(db, project_id, requirement_text, top_k=2, doc_types=["prd", "cases"])
    if similar:
        parts.append("【相似历史需求/用例（口径对齐参考）】\n" + build_rag_context(similar))
    defects = retrieve(db, project_id, requirement_text, top_k=2, doc_types=["defects"])
    if defects:
        parts.append("【历史缺陷（回归重点）】\n" + build_rag_context(defects))
    return "\n\n".join(parts)


def build_case_context(db, project_id: int, requirement_text: str, top_k: int = 2) -> str:
    """组装注入用例生成（功能/API/UI）的 RAG 上下文（B12：三类知识注入）。

    第一类：相关模块-功能的系统操作手册（prd，业务操作流程）
    第二类：相关模块-功能的历史基线用例（cases，复用与口径参考）
    第三类：相关模块-功能的上线检查清单（checklist，历史线上问题检查点，严格参考）
    """
    parts: list[str] = []
    manual = retrieve(db, project_id, requirement_text, top_k=top_k, doc_types=["prd"])
    if manual:
        parts.append("【相关模块操作手册（业务操作流程参考）】\n" + build_rag_context(manual))
    baseline = retrieve(db, project_id, requirement_text, top_k=top_k, doc_types=["cases"])
    if baseline:
        parts.append("【相关模块历史基线用例（复用与口径参考）】\n" + build_rag_context(baseline))
    checklist = retrieve(db, project_id, requirement_text, top_k=top_k, doc_types=["checklist"])
    if checklist:
        parts.append("【上线检查清单（历史线上问题检查点，必须严格参考）】\n" + build_rag_context(checklist))
    return "\n\n".join(parts)


def project_id_from(task) -> int | None:
    """从任务 meta 或环境变量解析 project_id（知识库项目归属）。"""
    import os

    if task and task.meta and task.meta.get("project_id"):
        try:
            return int(task.meta["project_id"])
        except (TypeError, ValueError):
            pass
    try:
        v = os.environ.get("AI_TEST_PROJECT_ID")
        return int(v) if v else None
    except (TypeError, ValueError):
        return None

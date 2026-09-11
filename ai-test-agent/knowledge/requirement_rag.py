"""需求侧 RAG（P2）：相似需求/历史用例/缺陷检索 → 注入解析与计划生成上下文。

- 相似需求复用：检索 prd/cases 类知识，作为需求解析的 few-shot 口径对齐
- 缺陷关联：检索 defects 类知识，作为回归重点与风险提示
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

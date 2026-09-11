"""召回质量评估（T43）：召回率 / 忠实度 / 完整性 / 抗幻觉 四指标。

- 评估脚本：给定 query + 期望命中的分片（gold），对比实际召回 top_k
- 指标定义：
  - 召回率 recall@k：gold 命中的比例
  - 忠实度 faithfulness：回答中事实性句子中能被召回内容支持的占比（规则近似：词级重叠）
  - 完整性 completeness：召回内容覆盖 query 主题词的占比
  - 抗幻觉 anti_hallucination：无召回时回答是否拒绝编造
- 目标：召回准确率 ≥ 75%（以 recall@3 计）
"""
from __future__ import annotations

from .chunking import tokenize


def recall_at_k(gold_ids: set[int], hits: list[dict], k: int | None = None) -> float:
    """gold 中命中前 k 的比例。"""
    k = k or len(hits)
    if not gold_ids:
        return 0.0
    hit_ids = {h["chunk_id"] for h in hits[:k]}
    return len(gold_ids & hit_ids) / len(gold_ids)


def completeness(query: str, hits: list[dict]) -> float:
    """query 主题词被召回内容覆盖的比例。"""
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return 0.0
    covered = set()
    for h in hits:
        covered |= set(tokenize(h["content"]))
    return len(q_tokens & covered) / len(q_tokens)


def faithfulness(answer: str, hits: list[dict]) -> float:
    """回答内容能被召回内容支持的近似比例（词级重叠，真实评估由 LLM 判定）。"""
    a_tokens = set(tokenize(answer))
    if not a_tokens:
        return 1.0
    src_tokens: set[str] = set()
    for h in hits:
        src_tokens |= set(tokenize(h["content"]))
    supported = len(a_tokens & src_tokens)
    return supported / len(a_tokens)


def anti_hallucination(answer: str, hits: list[dict], has_evidence: bool) -> float:
    """无召回时回答应明确说明无依据（拒绝编造）→ 1.0；否则 0.0。"""
    if has_evidence:
        return 1.0 if answer.strip() else 0.0
    refuse = any(k in answer for k in ("没有相关", "无相关", "未检索到", "无法回答", "上下文"))
    return 1.0 if refuse else 0.0


def evaluate_recall(gold_map: dict[str, dict], retrieve_fn, k: int = 3) -> dict:
    """整体评估：对每条 query 跑召回并与 gold 对比，输出四指标汇总。"""
    recall_scores, complete_scores, faith_scores, anti_scores = [], [], [], []
    cases: list[dict] = []
    for query, spec in gold_map.items():
        hits = retrieve_fn(query, k)
        gold = set(spec.get("gold_ids", []))
        has_evidence = bool(hits)
        # 对抗样例：期望无召回（query 不在知识库内）
        if spec.get("expect_empty"):
            anti_scores.append(anti_hallucination(spec.get("answer", ""), hits, has_evidence))
            recall_scores.append(1.0 if not hits else 0.0)
            cases.append({"query": query, "hit_count": len(hits), "expected": "empty", "ok": not hits})
            continue
        rec = recall_at_k(gold, hits, k)
        comp = completeness(query, hits)
        faith = faithfulness(spec.get("answer", ""), hits)
        recall_scores.append(rec)
        complete_scores.append(comp)
        faith_scores.append(faith)
        anti_scores.append(1.0)
        cases.append({"query": query, "hit_count": len(hits), "recall@k": round(rec, 3),
                      "completeness": round(comp, 3), "faithfulness": round(faith, 3), "ok": rec >= 0.75})

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    return {
        "recall@k": _avg(recall_scores),
        "completeness": _avg(complete_scores),
        "faithfulness": _avg(faith_scores),
        "anti_hallucination": _avg(anti_scores),
        "cases": cases,
        "pass": _avg(recall_scores) >= 0.75,
    }

"""Grader（T45）：用例质量打分器（覆盖度/正确性/稳定性 1-5 分）。

- 规则模式（默认）：基于用例文本特征确定性打分，可复算、可校准
- LLM 模式：配置 AI_TEST_LLM_API_KEY 后由 LLM 打分（同一 prompt 契约）
- 输出 graded 结果并可与人工标注 labels 对比一致性（agreement）
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from web.models import EvalSample

_SEVERITY_WORDS = {
    "coverage": ["正常", "异常", "边界", "空", "超长", "不存在", "非法", "并发", "负数", "错误", "缺失", "类型"],
    "correctness": ["断言", "200", "201", "401", "404", "405", "422", "拒绝", "崩溃", "返回", "抛"],
    "stability": ["重试", "超时", "并发", "防抖", "幂等", "锁定", "刷新", "重复", "加载", "缓存"],
}


def _score(text: str, kind: str) -> int:
    words = _SEVERITY_WORDS[kind]
    hits = sum(1 for w in words if w in text)
    # 覆盖度：具体可执行的用例（有方法/参数/操作）基础 4，含边界要素再累加
    if kind == "coverage":
        concrete = any(s in text for s in ("→", "{", "POST", "GET", "输入", "点击", "断言", "打开"))
        base = 4 if concrete else 3
        return min(5, base + hits)
    # 正确性：有断言/状态码/错误处理则高
    if kind == "correctness":
        if hits >= 2:
            return 5
        if hits == 1:
            return 4
        return 3
    # 稳定性：正常用例本身稳定（基础 4），含稳定性要素再累加
    if kind == "stability":
        return min(5, 4 + hits)
    return 3


def grade_case(test_case: str) -> dict:
    """对单个用例打分：{coverage, correctness, stability, score, level}。"""
    coverage = _score(test_case, "coverage")
    correctness = _score(test_case, "correctness")
    stability = _score(test_case, "stability")
    score = round((coverage + correctness + stability) / 3, 2)
    level = "A" if score >= 4.5 else ("B" if score >= 3.5 else ("C" if score >= 2.5 else "D"))
    return {"coverage": coverage, "correctness": correctness, "stability": stability,
            "score": score, "level": level}


def run_grader(db: Session, limit: int | None = None, use_llm: bool = False) -> dict:
    """对全部 pending 样本打分；返回汇总与一致性（vs 人工标注）。"""
    q = db.query(EvalSample).filter(EvalSample.status == "pending")
    if limit:
        q = q.limit(limit)
    samples = q.all()
    if not samples:
        return {"graded": 0, "agreement": None, "report": {}}

    llm = None
    if use_llm:
        from core.llm import create_llm

        llm = create_llm()

    total_dev = 0.0
    pairs = 0
    levels = {"A": 0, "B": 0, "C": 0, "D": 0}
    for s in samples:
        if use_llm and llm is not None:
            try:
                raw = llm.chat_json(
                    "你是用例质量评审专家，只输出 JSON：{\"coverage\":1-5,\"correctness\":1-5,\"stability\":1-5}",
                    f"用例：{s.test_case}\n类别：{s.category}",
                )
                graded = {"coverage": int(raw.get("coverage", 3)), "correctness": int(raw.get("correctness", 3)),
                          "stability": int(raw.get("stability", 3))}
            except Exception:  # noqa: BLE001
                graded = grade_case(s.test_case)
        else:
            graded = grade_case(s.test_case)
        graded["score"] = round(sum(graded[k] for k in ("coverage", "correctness", "stability")) / 3, 2)
        graded["level"] = "A" if graded["score"] >= 4.5 else ("B" if graded["score"] >= 3.5 else ("C" if graded["score"] >= 2.5 else "D"))
        s.graded = graded
        s.status = "graded"
        levels[graded["level"]] += 1

        # 与人工标注一致性：各维度绝对差均值（越小越一致）
        if s.labels:
            dev = sum(abs(graded[k] - s.labels.get(k, 3)) for k in ("coverage", "correctness", "stability")) / 3
            total_dev += dev
            pairs += 1
    db.commit()

    return {
        "graded": len(samples),
        "agreement": round(1 - total_dev / (4 * pairs), 3) if pairs else None,  # 1=完全一致
        "report": {"levels": levels, "avg_score": round(sum(s.graded["score"] for s in samples) / len(samples), 2)},
    }

"""Trace 可观测（T46）：Agent 执行轨迹落盘（JSONL）+ DB 索引 + 查询回放。

- 每次任务创建 trace_id，步骤事件写入 data/traces/<trace_id>.jsonl（可回放）
- 同时落 trace_entries 表（可查询）
- 任一步骤可溯源：agent/step/input/output/latency/level
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from sqlalchemy import desc
from sqlalchemy.orm import Session

from web.models import TraceEntry

_TRACES_DIR = Path(__file__).resolve().parent.parent / "data" / "traces"


def new_trace_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]


def trace_log(db: Session | None, trace_id: str, agent: str, step: str, level: str = "info",
              input_: str = "", output: str = "", latency_ms: int = 0) -> None:
    """记录一步轨迹：写 JSONL（始终）+ DB（可用时）。"""
    event = {
        "ts": time.time(),
        "trace_id": trace_id,
        "agent": agent,
        "step": step,
        "level": level,
        "input": input_[:2000],
        "output": output[:2000],
        "latency_ms": latency_ms,
    }
    try:
        _TRACES_DIR.mkdir(parents=True, exist_ok=True)
        with open(_TRACES_DIR / f"{trace_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    if db is not None:
        try:
            db.add(TraceEntry(trace_id=trace_id, agent=agent, step=step, level=level,
                              input=input_[:2000], output=output[:2000], latency_ms=latency_ms))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()


def query_trace(db: Session, trace_id: str) -> dict:
    """按 trace_id 查询全部步骤（DB 优先，缺失时读 JSONL）。"""
    rows = db.query(TraceEntry).filter(TraceEntry.trace_id == trace_id).order_by(TraceEntry.id).all()
    if rows:
        return {"trace_id": trace_id, "steps": [
            {"agent": r.agent, "step": r.step, "level": r.level, "input": r.input,
             "output": r.output, "latency_ms": r.latency_ms, "ts": r.created_at.isoformat()}
            for r in rows
        ]}
    # JSONL 回放
    f = _TRACES_DIR / f"{trace_id}.jsonl"
    if not f.is_file():
        return {"trace_id": trace_id, "steps": [], "error": "trace 不存在"}
    steps = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"trace_id": trace_id, "steps": steps}


def recent_traces(db: Session, limit: int = 20) -> list[dict]:
    rows = db.query(TraceEntry.trace_id).distinct().order_by(desc(TraceEntry.id)).limit(limit).all()
    return [r[0] for r in rows]

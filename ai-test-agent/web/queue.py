"""异步任务队列（T38：任务提交→队列→Runner 隔离执行→状态实时可查）。

- 内存 asyncio 队列 + 后台 worker 线程池（避免阻塞事件循环）
- 任务状态流转：pending → running → done / failed
- 提交接口返回任务 ID，前端轮询状态；也提供同步等待（供测试）
"""
from __future__ import annotations

import asyncio
import threading
import traceback
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Task

_QUEUE: asyncio.Queue[int] | None = None
_WORKER: asyncio.Task | None = None
_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="task-runner")
    return _executor


def start_worker() -> None:
    """应用启动时调用：创建队列并启动消费循环。"""
    global _QUEUE, _WORKER
    if _WORKER is not None and not _WORKER.done():
        return
    _QUEUE = asyncio.Queue()

    async def _loop():
        assert _QUEUE is not None
        while True:
            task_id = await _QUEUE.get()
            loop = asyncio.get_running_loop()
            loop.run_in_executor(_get_executor(), _execute_task, task_id)

    _WORKER = asyncio.create_task(_loop())


def enqueue(task_row_id: int) -> None:
    """把 DB 中的任务提交进队列（worker 未启动则直接线程执行，保证单测可跑）。"""
    if _QUEUE is None or _WORKER is None or _WORKER.done():
        threading.Thread(target=_execute_task, args=(task_row_id,), daemon=True).start()
        return
    _QUEUE.put_nowait(task_row_id)


def _execute_task(task_row_id: int) -> None:
    """独立线程执行：读任务 → runner.run_task → 回写状态/报告/指标/Trace。"""
    from . import runner

    db: Session = SessionLocal()
    trace_id = ""
    try:
        task_row = db.get(Task, task_row_id)
        if task_row is None:
            return
        trace_id = task_row.trace_id or f"task_{task_row.id}"
        task_row.trace_id = trace_id
        task_row.status = "running"
        task_row.started_at = datetime.now()
        db.commit()

        from eval.trace import trace_log

        trace_log(db, trace_id, "queue", "任务进入执行队列", "info", input_=task_row.title)
        trace_log(db, trace_id, "runner", f"{task_row.task_type} 执行开始", "info",
                  input_=(task_row.requirement or "")[:500])

        normalized = runner.run_task(
            requirement=task_row.requirement or "测试任务",
            task_type=task_row.task_type,
            meta=dict(task_row.meta or {}),
        )
        runner.persist_report(db, task_row, normalized, report_type=task_row.task_type)
        trace_log(db, trace_id, "runner", f"{task_row.task_type} 执行完成",
                  "info" if normalized["status"] == "done" else "error",
                  output=f"通过 {normalized.get('passed', 0)} / {normalized.get('total', 0)}",
                  latency_ms=0)
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        task_row = db.get(Task, task_row_id)
        if task_row is not None:
            task_row.status = "failed"
            task_row.error = f"{e}\n{traceback.format_exc()[-800:]}"
            task_row.finished_at = datetime.now()
            db.commit()
            try:
                from eval.trace import trace_log

                trace_log(db, trace_id or f"task_{task_row.id}", "runner", "执行失败", "error", output=str(e)[:500])
            except Exception:  # noqa: BLE001
                pass
    finally:
        db.close()

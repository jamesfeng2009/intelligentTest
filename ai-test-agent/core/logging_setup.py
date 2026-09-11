"""结构化日志。

约定：
- 每条日志带 task_id / agent / module 维度，便于按任务聚合复盘（Trace）。
- 输出 JSON 行，可直接被日志系统采集。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from contextvars import ContextVar
from datetime import datetime

_current_task: ContextVar[str | None] = ContextVar("task_id", default=None)
_current_agent: ContextVar[str | None] = ContextVar("agent", default=None)


def set_task_context(task_id: str | None) -> None:
    _current_task.set(task_id)


def set_agent_context(agent: str | None) -> None:
    _current_agent.set(agent)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "task_id": _current_task.get(),
            "agent": _current_agent.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        for key in ("event", "detail"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("ai_test")
    if root.handlers:  # 避免重复初始化
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # 抑制第三方噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai_test.{name}")


def log_event(logger: logging.Logger, event: str, detail: dict | None = None) -> None:
    """带事件名的结构化日志。"""
    kwargs: dict = {"extra": {"event": event}}
    if detail is not None:
        kwargs["extra"]["detail"] = detail
    logger.info("[%s] %s", event, json.dumps(detail, ensure_ascii=False) if detail else "", **kwargs)

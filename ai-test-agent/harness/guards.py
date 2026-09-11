"""护栏规则 —— 代码硬拦截。

拦截点：
1. 危险 shell 命令（rm -rf / 管道到 sh / 删除项目外文件等）
2. 重试次数超限
3. 执行超时
4. LLM 输出 schema 校验（超约束丢弃）

护栏是"默认拒绝"策略：未通过校验的动作一律不允许。
"""
from __future__ import annotations

import re
import signal
from dataclasses import dataclass, field
from typing import Any, Callable

from core.config import get_settings
from core.logging_setup import get_logger

logger = get_logger("guards")

# 危险命令模式（黑名单）
_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-[a-z]*[rf]", re.I),          # rm -rf
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r"\b:\(\)\s*\{", re.I),                # fork bomb
    re.compile(r"\bsudo\s+rm\b", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\breboot\b", re.I),
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b", re.I),    # curl | sh
    re.compile(r"\bgit\s+push\s+--force\b", re.I),
    re.compile(r"\bmv\s+/\s+", re.I),
]


class GuardViolation(Exception):
    pass


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    retries_left: int = 0


@dataclass
class CommandGuard:
    """命令执行护栏：拦截危险命令 + 超时控制。"""

    timeout: int = 60
    max_output: int = 200_000

    def check(self, command: str) -> GuardResult:
        for pat in _DANGEROUS_PATTERNS:
            if pat.search(command):
                return GuardResult(allowed=False, reason=f"命中危险命令模式: {pat.pattern}")
        return GuardResult(allowed=True)


class RetryGuard:
    """重试上限控制：最多 max_retries 次，超限抛 GuardViolation。"""

    def __init__(self, max_retries: int | None = None) -> None:
        self.max_retries = max_retries if max_retries is not None else get_settings().max_retry
        self._attempts: dict[str, int] = {}

    def reset(self, key: str) -> None:
        self._attempts[key] = 0

    def before(self, key: str) -> GuardResult:
        n = self._attempts.get(key, 0)
        if n >= self.max_retries:
            return GuardResult(allowed=False, reason=f"重试超限（>{self.max_retries} 次）: {key}", retries_left=0)
        return GuardResult(allowed=True, retries_left=self.max_retries - n)

    def after_failure(self, key: str) -> None:
        self._attempts[key] = self._attempts.get(key, 0) + 1
        logger.warning("护栏：%s 已失败 %s/%s 次", key, self._attempts[key], self.max_retries)


class TimeoutGuard:
    """超时控制：装饰器或 with 语句。"""

    def __init__(self, seconds: int | None = None) -> None:
        self.seconds = seconds if seconds is not None else get_settings().default_timeout_s

    def __enter__(self) -> "TimeoutGuard":
        if hasattr(signal, "SIGALRM"):  # Unix
            signal.signal(signal.SIGALRM, self._handler)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc: Any) -> None:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

    def _handler(self, *_a: Any) -> None:
        raise GuardViolation(f"执行超时（>{self.seconds}s）")


class SchemaGuard:
    """LLM 输出 schema 校验：不符合约束的输出直接丢弃并返回 None。"""

    def __init__(self, required_keys: list[str], allowed_keys: list[str] | None = None) -> None:
        self.required_keys = required_keys
        self.allowed_keys = allowed_keys

    def validate(self, obj: dict) -> dict | None:
        if not isinstance(obj, dict):
            logger.warning("护栏：输出非对象，丢弃")
            return None
        for k in self.required_keys:
            if k not in obj:
                logger.warning("护栏：缺少必需字段 %s，丢弃输出", k)
                return None
        if self.allowed_keys is not None:
            extra = set(obj) - set(self.allowed_keys)
            if extra:
                logger.warning("护栏：存在超约束字段 %s，丢弃输出", extra)
                return None
        return obj


# ================= C2 禁止行为清单（总控护栏） =================
# 编排者 / 子 Agent 的显式禁止行为（Harness 护栏规则的"越权/走捷径"维度）
FORBIDDEN_BEHAVIORS: tuple[str, ...] = (
    "禁止直接修改需求工单/需求原文（需求是测试依据，只读）",
    "禁止跳过验证阶段直接报告成功（结果必须来自真实执行/校验）",
    "禁止虚构执行结果、通过率或测试产物（数据必须可追溯）",
    "禁止未经审批执行发布、删除、外部写入等高危动作",
    "禁止越权调用其它子 Agent 的职能（Analyzer 不写码、Implementer 不评审自己）",
    "禁止为通过测试而修改被测代码或弱化断言",
)


def check_forbidden(behavior: str, extra: dict | None = None) -> bool:
    """C2 禁止行为护栏：命中清单返回 False（禁止），并记录违规事件。

    用于编排者在调度前后/汇总前校验动作合法性；非命中返回 True。
    """
    for rule in FORBIDDEN_BEHAVIORS:
        keyword = rule.split("禁止")[-1].split("（")[0].strip()[:8]
        if keyword and keyword in behavior:
            logger.error("C2 禁止行为命中：%s | 行为: %s | 详情: %s", rule, behavior, extra or {})
            return False
    return True

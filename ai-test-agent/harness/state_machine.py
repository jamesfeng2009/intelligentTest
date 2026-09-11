"""Harness 状态机 —— 代码硬约束。

核心职责：
1. 定义合法状态与转移表（禁止跳步）
2. 提供状态转移校验（非法转移抛异常）
3. 与 workflows/ 中的 LangGraph 执行图保持一致（图节点即状态动作）

状态定义（对齐项目计划 §2.2）：
IDLE → ANALYZE → WAIT_APPROVAL → SETUP → (UI_TEST|API_TEST|WHITEBOX_TEST) → VERIFY → REPORT → DONE
"""
from __future__ import annotations

from enum import Enum
from typing import Callable


class State(Enum):
    IDLE = "IDLE"
    ANALYZE = "ANALYZE"
    WAIT_APPROVAL = "WAIT_APPROVAL"
    SETUP = "SETUP"
    UI_TEST = "UI_TEST"
    API_TEST = "API_TEST"
    WHITEBOX_TEST = "WHITEBOX_TEST"
    FUNCTIONAL_TEST = "FUNCTIONAL_TEST"    # 功能测试用例生成（A 模型）
    CASE_REVIEW = "CASE_REVIEW"            # 用例独立评审（B 模型）
    VERIFY = "VERIFY"
    REPORT = "REPORT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# 合法转移表：current -> allowed next
_TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.ANALYZE, State.CANCELLED},
    State.ANALYZE: {State.WAIT_APPROVAL, State.IDLE, State.FAILED},          # 计划审批前可回退修改
    State.WAIT_APPROVAL: {State.SETUP, State.ANALYZE, State.FAILED},         # 审批通过→SETUP；驳回→ANALYZE
    State.SETUP: {State.UI_TEST, State.API_TEST, State.WHITEBOX_TEST, State.FUNCTIONAL_TEST, State.FAILED},
    # 多 Agent 串行：每个子 Agent 一个 TEST 状态，允许顺序推进后统一进入 VERIFY
    State.UI_TEST: {State.API_TEST, State.WHITEBOX_TEST, State.FUNCTIONAL_TEST, State.VERIFY, State.FAILED},
    State.API_TEST: {State.UI_TEST, State.WHITEBOX_TEST, State.FUNCTIONAL_TEST, State.VERIFY, State.FAILED},
    State.WHITEBOX_TEST: {State.FUNCTIONAL_TEST, State.VERIFY, State.FAILED},
    # 功能用例生成 → 独立评审（B 模型）；评审后进入其它测试或 VERIFY
    State.FUNCTIONAL_TEST: {State.CASE_REVIEW, State.VERIFY, State.FAILED},
    State.CASE_REVIEW: {State.VERIFY, State.FAILED},
    State.VERIFY: {State.REPORT, State.VERIFY, State.FAILED},                # VERIFY->VERIFY = 修复重试
    State.REPORT: {State.DONE, State.VERIFY, State.FAILED},                  # 报告确认后可打回重试
    State.DONE: set(),
    State.FAILED: {State.SETUP, State.ANALYZE},                              # 允许失败后重跑（重新进入流程）
    State.CANCELLED: set(),
}


class IllegalTransitionError(Exception):
    def __init__(self, current: State, target: State):
        super().__init__(f"非法状态转移: {current.value} -> {target.value}（Harness 禁止跳步）")
        self.current = current
        self.target = target


class StateMachine:
    """显式状态机：跟踪状态 + 校验转移 + 记录轨迹。"""

    def __init__(self, initial: State = State.IDLE) -> None:
        self._state = initial
        self._history: list[tuple[State, State, str]] = []  # (from, to, reason)

    @property
    def state(self) -> State:
        return self._state

    def can_transition(self, target: State) -> bool:
        return target in _TRANSITIONS.get(self._state, set())

    def transition(self, target: State, reason: str = "") -> State:
        if not self.can_transition(target):
            raise IllegalTransitionError(self._state, target)
        self._history.append((self._state, target, reason))
        self._state = target
        return self._state

    def history(self) -> list[dict]:
        return [
            {"from": f.value, "to": t.value, "reason": r}
            for f, t, r in self._history
        ]

    def is_terminal(self) -> bool:
        return self._state in (State.DONE, State.FAILED, State.CANCELLED)


# 状态动作注册（供 LangGraph 图节点与状态机对应）
NodeAction = Callable[[dict], dict]
_NODE_REGISTRY: dict[str, NodeAction] = {}


def register_node(name: str):
    def deco(fn: NodeAction) -> NodeAction:
        _NODE_REGISTRY[name] = fn
        return fn
    return deco


def get_node(name: str) -> NodeAction | None:
    return _NODE_REGISTRY.get(name)

"""审批节点管理 —— 四个中断点（Harness 人工闸门）。

中断点：
1. 计划审批（ANALYZE 完成后）：测试范围/策略/风险点是否合理
2. 代码审查（测试代码生成后）：是否有危险操作/跳步/越权
3. 失败分类（执行失败后）：脚本错误 or 真实 Bug，是否修复重试
4. 报告确认（最终报告生成后）：结果是否准确，是否补充测试

设计：审批可通过 auto_approve=True（自动化演示）或人工回调（真实接入）驱动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from core.logging_setup import get_logger

logger = get_logger("approvals")


class ApprovalDecision(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"


@dataclass
class ApprovalRequest:
    point: str          # 中断点名称
    payload: dict       # 待审批内容（计划/代码/分类/报告）
    status: str = "pending"
    decision: ApprovalDecision | None = None
    comment: str = ""
    reviewer: str = "auto"

    def to_dict(self) -> dict:
        return {
            "point": self.point,
            "status": self.status,
            "decision": self.decision.value if self.decision else None,
            "comment": self.comment,
            "reviewer": self.reviewer,
            "payload_summary": self._summarize(),
        }

    def _summarize(self) -> str:
        p = self.payload
        keys = list(p.keys())[:6]
        return ", ".join(f"{k}: {str(p[k])[:60]}" for k in keys)


ApprovalCallback = Callable[[ApprovalRequest], ApprovalDecision]


class ApprovalManager:
    """管理一次运行中的所有审批请求。"""

    def __init__(self, auto_approve: bool = False, callback: ApprovalCallback | None = None) -> None:
        self.auto_approve = auto_approve
        self.callback = callback
        self._requests: list[ApprovalRequest] = []
        self._pending: list[ApprovalRequest] = []

    def request(self, point: str, payload: dict) -> ApprovalRequest:
        req = ApprovalRequest(point=point, payload=payload)
        self._requests.append(req)
        if self.auto_approve:
            req.status = "approved"
            req.decision = ApprovalDecision.APPROVED
            req.reviewer = "auto"
            logger.info("审批[%s] 自动通过（auto_approve）", point)
            return req
        if self.callback is not None:
            decision = self.callback(req)
            self._resolve(req, decision, reviewer="callback")
            return req
        # 无回调且非自动：进入待审批队列（由外部驱动）
        self._pending.append(req)
        logger.info("审批[%s] 进入待审批队列，等待人工确认", point)
        return req

    def _resolve(self, req: ApprovalRequest, decision: ApprovalDecision, reviewer: str = "human", comment: str = "") -> None:
        req.status = decision.value
        req.decision = decision
        req.reviewer = reviewer
        req.comment = comment
        if req in self._pending:
            self._pending.remove(req)
        logger.info("审批[%s] 决议=%s reviewer=%s comment=%s", req.point, decision.value, reviewer, comment)

    def approve(self, point: str, comment: str = "") -> None:
        """外部（人工/API）审批通过。"""
        req = self._find_pending(point)
        self._resolve(req, ApprovalDecision.APPROVED, reviewer="human", comment=comment)

    def reject(self, point: str, comment: str = "") -> None:
        req = self._find_pending(point)
        self._resolve(req, ApprovalDecision.REJECTED, reviewer="human", comment=comment)

    def _find_pending(self, point: str) -> ApprovalRequest:
        for req in self._pending:
            if req.point == point:
                return req
        raise KeyError(f"没有待审批的节点: {point}")

    def pending_points(self) -> list[str]:
        return [r.point for r in self._pending]

    def all(self) -> list[dict]:
        return [r.to_dict() for r in self._requests]

    def is_approved(self, point: str) -> bool:
        for r in self._requests:
            if r.point == point:
                return r.decision == ApprovalDecision.APPROVED
        return False

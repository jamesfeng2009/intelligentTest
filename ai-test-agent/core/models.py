"""核心数据模型（对齐项目计划 §3.1）。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class TestTask:
    """一次测试任务的完整状态载体。"""

    requirement: str                       # 原始需求
    task_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6])
    test_scope: dict = field(default_factory=dict)        # 测试范围（UI/API/白盒各测什么）
    status: str = "IDLE"                   # 状态机当前状态
    base_commit: Optional[str] = None      # 白盒基线 commit
    target_commit: Optional[str] = None    # 白盒目标 commit
    repo_path: Optional[str] = None        # 代码仓库路径
    created_at: datetime = field(default_factory=datetime.now)
    artifacts_dir: str = ""                # 产物目录
    meta: dict = field(default_factory=dict)             # 扩展信息（LLM 配置、审批策略等）

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "requirement": self.requirement,
            "test_scope": self.test_scope,
            "status": self.status,
            "base_commit": self.base_commit,
            "target_commit": self.target_commit,
            "repo_path": self.repo_path,
            "created_at": self.created_at.isoformat(),
            "artifacts_dir": self.artifacts_dir,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TestTask":
        return cls(
            requirement=d.get("requirement", ""),
            task_id=d.get("task_id", ""),
            test_scope=d.get("test_scope", {}),
            status=d.get("status", "IDLE"),
            base_commit=d.get("base_commit"),
            target_commit=d.get("target_commit"),
            repo_path=d.get("repo_path"),
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else datetime.now(),
            artifacts_dir=d.get("artifacts_dir", ""),
            meta=d.get("meta", {}),
        )


@dataclass
class TestPlan:
    """总控 Agent 产出的结构化测试计划。"""

    scope: list[str]                       # ["ui", "api", "whitebox"]
    strategy: str
    risk_points: list[str]
    scenarios: list[dict]
    estimates: dict
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "strategy": self.strategy,
            "risk_points": self.risk_points,
            "scenarios": self.scenarios,
            "estimates": self.estimates,
            "summary": self.summary,
        }


@dataclass
class TestResult:
    """一次用例执行结果。"""

    name: str
    status: str                            # passed / failed / error / skipped
    duration_ms: float = 0.0
    error: str = ""
    category: str = ""                     # 失败分类（Verifier 填）
    evidence: dict = field(default_factory=dict)         # 截图/日志/请求响应

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "category": self.category,
            "evidence": self.evidence,
        }

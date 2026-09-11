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


# 功能测试用例合法分类
FUNCTIONAL_CATEGORIES = ("normal", "negative", "boundary", "security")


@dataclass
class FunctionalCase:
    """一条功能测试用例（业务场景级，从需求 + 接口文档推导，不依赖代码）。"""

    id: str                                # FC-001
    feature: str                           # 所属功能点（用户登录 / 商品管理）
    title: str                             # 场景标题
    category: str                          # normal / negative / boundary / security
    preconditions: str                     # 前置条件
    steps: list[str]                       # 操作步骤
    test_data: dict                        # 测试数据
    expected: str                          # 预期结果
    involved_endpoints: list[dict]         # 涉及接口 [{method, path}]（须存在于接口文档）
    traceability: dict = field(default_factory=dict)   # 需求追溯 {"requirement": "..."}

    def to_dict(self) -> dict:
        return {
            "id": self.id, "feature": self.feature, "title": self.title, "category": self.category,
            "preconditions": self.preconditions, "steps": self.steps, "test_data": self.test_data,
            "expected": self.expected, "involved_endpoints": self.involved_endpoints,
            "traceability": self.traceability,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FunctionalCase":
        return cls(
            id=str(d.get("id", "")), feature=str(d.get("feature", "")), title=str(d.get("title", "")),
            category=str(d.get("category", "normal")),
            preconditions=str(d.get("preconditions", "")),
            steps=list(d.get("steps", [])),
            test_data=dict(d.get("test_data", {})),
            expected=str(d.get("expected", "")),
            involved_endpoints=list(d.get("involved_endpoints", [])),
            traceability=dict(d.get("traceability", {})),
        )


@dataclass
class FunctionalReview:
    """B 模型对 A 模型功能用例的独立评审结果。"""

    overall: str                           # pass / issues_found
    summary: str
    findings: list[dict]                   # [{case_id, verdict, severity, issue, suggestion}]
    missing_scenarios: list[str]           # 需求中未被覆盖的场景
    revised_cases: list[dict] = field(default_factory=list)   # 按评审意见修订后的用例
    reviewed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "overall": self.overall, "summary": self.summary, "findings": self.findings,
            "missing_scenarios": self.missing_scenarios, "revised_cases": self.revised_cases,
            "reviewed_at": self.reviewed_at,
        }


@dataclass
class RequirementItem:
    """需求条目（L2/L3 解析产物）：一个可测试的功能点。"""

    id: str                                # RQ-001
    title: str                             # 功能点名称
    desc: str                              # 描述
    acceptance: list[str]                  # 验收标准
    rules: list[str]                       # 业务规则
    constraints: list[str]                 # 约束（非功能/边界）
    priority: str = "P1"                   # P0/P1/P2
    involved_endpoints: list[dict] = field(default_factory=list)   # [{method, path}]
    source: str = ""                       # 章节引用（如 "3.2 用户登录"）

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "desc": self.desc, "acceptance": self.acceptance,
            "rules": self.rules, "constraints": self.constraints, "priority": self.priority,
            "involved_endpoints": self.involved_endpoints, "source": self.source,
        }


@dataclass
class RequirementParseResult:
    """需求三级解析结果：结构化条目 + 摘要 + 风险。"""

    items: list[RequirementItem]
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)     # L1 文档结构 [{title, level}]

    def to_dict(self) -> dict:
        return {
            "summary": self.summary, "risks": self.risks, "sections": self.sections,
            "items": [i.to_dict() for i in self.items],
        }

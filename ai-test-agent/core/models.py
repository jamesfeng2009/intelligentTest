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

    # ---- A17 结构化映射 + 双向追溯（步骤序号化） ----
    def step_refs(self) -> list[str]:
        """双向追溯键：{case_id}.{step_no}，如 FC-001.2。脚本注释与执行回链均使用该键。"""
        return [f"{self.id}.{i + 1}" for i in range(len(self.steps))]

    def numbered_steps(self) -> list[str]:
        """步骤序号化：在每步前加 {case_id}.{step_no} 前缀，如 'FC-001.2 构造正确用户名'。"""
        return [f"{self.id}.{i + 1} {s}".rstrip() for i, s in enumerate(self.steps)]

    def structured_steps(self) -> list[dict]:
        """结构化映射（A17）：[序号, 动作, 目标, 数据] 四元结构化数组。"""
        targets = "; ".join(f"{e['method']} {e['path']}" for e in self.involved_endpoints) or "-"
        return [
            {"seq": f"{self.id}.{i + 1}", "action": s, "target": targets, "data": self.test_data}
            for i, s in enumerate(self.steps)
        ]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "feature": self.feature, "title": self.title, "category": self.category,
            "preconditions": self.preconditions, "steps": self.steps, "test_data": self.test_data,
            "expected": self.expected, "involved_endpoints": self.involved_endpoints,
            "traceability": self.traceability,
            "step_refs": self.step_refs(),            # A17：双向追溯键
            "numbered_steps": self.numbered_steps(),  # A17：序号化步骤
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
    # A3 分层多趟提取 - Pass3 依赖面
    upstream_systems: list[str] = field(default_factory=list)      # 上游系统/服务
    downstream_systems: list[str] = field(default_factory=list)    # 下游系统/服务
    data_compat_rules: list[str] = field(default_factory=list)     # 数据兼容规则（字段格式/枚举取值/兼容旧数据）
    # A15 需求条目级清单：预估用例数（正常/异常/边界/权限至少各 1 条，按功能复杂度估算）
    est_cases: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "desc": self.desc, "acceptance": self.acceptance,
            "rules": self.rules, "constraints": self.constraints, "priority": self.priority,
            "involved_endpoints": self.involved_endpoints, "source": self.source,
            "upstream_systems": self.upstream_systems,
            "downstream_systems": self.downstream_systems,
            "data_compat_rules": self.data_compat_rules,
            "est_cases": self.est_cases,
        }


@dataclass
class RequirementParseResult:
    """需求三级解析结果：结构化条目 + 摘要 + 风险（A3 多趟提取合并产物）。"""

    items: list[RequirementItem]
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)     # L1 文档结构 [{title, level}]
    edge_cases: list[str] = field(default_factory=list)    # A3 Pass4 异常面：边界与异常场景
    anomalies: list[str] = field(default_factory=list)     # A3 Pass4 异常面：异常处理与容错要求
    pass_report: dict = field(default_factory=dict)        # A3 每趟产出统计（防遗漏可追溯）

    def to_dict(self) -> dict:
        return {
            "summary": self.summary, "risks": self.risks, "sections": self.sections,
            "items": [i.to_dict() for i in self.items],
            "edge_cases": self.edge_cases, "anomalies": self.anomalies,
            "pass_report": self.pass_report,
        }

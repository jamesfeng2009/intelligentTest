"""Agent 注册表（P3）：Agent 管理串联测试流程的单一事实源。

三层关系：
- 注册表（build_registry）：平台内置 Agent 的声明式目录（name → AgentSpec）
- 配置面（agent_configs 表）：enabled 启停 / model 标注 / harness 参数，作为运行期覆盖层
- 路由面（orchestrator._dispatch）：查表 + enabled 过滤 + 前置条件检查 + 状态机节点 → 执行

自定义 Agent 接入 3 步：
1. 在 agents/ 下实现 run(task, plan, ctx) -> dict 的类（构造签名 (llm, artifacts)）
2. 在 build_registry 中注册 AgentSpec（name/entry/kind/state/requires/role/description）
3. （可选）在 web seed_agents 中补 agent_configs 行 —— 页面即可启停 / 标注模型

评审角色（functional_reviewer）是横切质量门：state=CASE_REVIEW、role=review（B 模型），
不占主线调度顺序，由 functional 主线在生成后调用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Type

if TYPE_CHECKING:  # 避免运行时循环依赖（harness 不依赖 agents）
    from harness.state_machine import State

logger = logging.getLogger("ai_test.agents.registry")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    entry: Type                 # 子 Agent 类，构造签名 (llm, artifacts)；编排者为 None
    kind: str                   # orchestrator/api/ui/whitebox/functional/review
    state: "State | None" = None   # 状态机节点；None=横切 Gate（不占主线节点）
    role: str = "main"          # main=生成（A 模型） / review=评审（B 模型）
    requires: tuple[str, ...] = ()   # 前置条件：repo_path / ui_base_url / openapi
    description: str = ""
    harness_default: dict = field(default_factory=dict)   # 默认 harness 参数（seed 用）
    mainline: bool = False      # True=参与主线调度顺序；False=编排者/横切 Gate


def build_registry() -> dict[str, AgentSpec]:
    """内置 Agent 目录（mainline=True 的顺序即主线调度顺序：api → ui → whitebox → functional）。"""
    from harness.state_machine import State

    from .api_tester import APITester
    from .functional_reviewer import FunctionalReviewer
    from .functional_tester import FunctionalTester
    from .ui_tester import UITester
    from .whitebox_tester import WhiteboxTester

    return {
        # 编排者（不参与主线调度，仅作为配置面条目）
        "orchestrator": AgentSpec(
            "orchestrator", None, "orchestrator", State.ANALYZE, role="main",
            description="编排者：需求解析 → 测试计划 → 审批 → 四线调度 → 报告",
            harness_default={"max_steps": 8, "approvals": ["计划审批", "发布审批"], "guards": ["越权防护", "重试上限"]},
        ),
        "api": AgentSpec(
            "api", APITester, "api", State.API_TEST, role="main", mainline=True,
            description="API 契约测试（OpenAPI 解析→用例→pytest→JUnit）",
            harness_default={"max_cases": 12, "guards": ["请求超时 10s", "状态码校验"]},
        ),
        "ui": AgentSpec(
            "ui", UITester, "ui", State.UI_TEST, role="main", requires=("ui_base_url",), mainline=True,
            description="UI 端到端（Midscene 脚本→真实浏览器→自愈重试）",
            harness_default={"max_steps": 20, "guards": ["定位超时 5s", "自愈重试 2"]},
        ),
        "whitebox": AgentSpec(
            "whitebox", WhiteboxTester, "whitebox", State.WHITEBOX_TEST, role="main",
            requires=("repo_path",), mainline=True,
            description="白盒变更分析（git diff→AST→影响面→单测）",
            harness_default={"max_functions": 20, "guards": ["危险模式检查", "变更范围限制"]},
        ),
        "functional": AgentSpec(
            "functional", FunctionalTester, "functional", State.FUNCTIONAL_TEST, role="main",
            requires=("openapi",), mainline=True,
            description="功能用例生成（A 模型：需求 + 接口文档 → 场景用例）",
            harness_default={"max_cases": 12, "guards": ["接口引用合法性", "必备字段完整性"]},
        ),
        # 横切质量门：B 模型独立评审（不参与主线顺序，由 functional 生成后调用）
        "functional_reviewer": AgentSpec(
            "functional_reviewer", FunctionalReviewer, "review", State.CASE_REVIEW, role="review",
            description="功能用例独立评审（B 模型：需求符合性 + 接口文档逻辑）——横切质量门",
            harness_default={"guards": ["程序硬检查", "语义评审", "修订回环"]},
        ),
        # 横切质量门：失败分类校验（不参与主线顺序，由 orchestrator._verify 调用）
        "verifier": AgentSpec(
            "verifier", None, "verify", State.VERIFY, role="main",
            description="失败分类校验（结果交叉校验 + 失败分类复核）——横切质量门",
            harness_default={"guards": ["结果交叉校验", "失败分类复核"]},
        ),
    }


def load_agent_configs() -> dict[str, dict]:
    """从 agent_configs 表读运行期配置（enabled/model/harness）。

    DB 不可用（CLI/demo/纯测试环境）时返回空 dict = 全部使用默认（enabled）。"""
    try:
        from web.db import SessionLocal
        from web.models import AgentConfig

        with SessionLocal() as db:
            return {
                r.name: {"enabled": bool(r.enabled), "model": r.model or "", "harness": r.harness or {}}
                for r in db.query(AgentConfig).all()
            }
    except Exception as e:  # noqa: BLE001 无 DB 环境不阻断调度
        logger.debug("agent_configs 不可用（CLI/demo 模式），使用默认配置: %s", e)
        return {}


def check_requires(task, req: str) -> bool:
    """前置条件检查：req ∈ repo_path / ui_base_url / openapi（task 字段或 meta）。"""
    meta = getattr(task, "meta", None) or {}
    if req == "repo_path":
        return bool(getattr(task, "repo_path", None))
    if req == "ui_base_url":
        return bool(meta.get("ui_base_url"))
    if req == "openapi":
        return bool(meta.get("openapi_path") or meta.get("base_url"))
    return True  # 未知条件默认放行

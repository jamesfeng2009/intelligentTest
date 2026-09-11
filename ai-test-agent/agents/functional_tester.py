"""功能测试用例 Agent（FunctionalTester）—— 生成侧（A 模型）。

流程：
1. 解析 OpenAPI 接口文档（程序，复用 api_tester.parse_openapi）
2. A 模型根据【需求 + 接口文档】生成业务功能场景级用例（正常/异常/边界/安全）
3. 程序校验：category 合法、involved_endpoints 必须存在于接口文档、必备字段非空
4. 校验不过丢弃；无合法用例时程序兜底（按接口语义）
5. 落盘 functional_cases.json / functional_cases.md，返回结构化结果

与 APITester 的区别：这里生成的是"业务功能场景"用例（业务逻辑 + 契约验证），
不直接渲染可执行 pytest；后续由 B 模型独立评审是否符合需求与接口文档逻辑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import FUNCTIONAL_CATEGORIES, FunctionalCase, TestPlan, TestTask
from harness.artifacts import ArtifactManager

logger = get_logger("functional_tester")


class FunctionalTester:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts

    def run(self, task: TestTask, plan: TestPlan, spec_path: str | None = None) -> dict:
        set_agent_context("functional_tester")
        spec_path = spec_path or task.meta.get("openapi_path") or str(Path(__file__).parent.parent / "examples" / "openapi_demo.yaml")

        # 1) 接口解析（程序）
        from agents.api_tester import parse_openapi

        endpoints = parse_openapi(spec_path)
        log_event(logger, "endpoints_parsed", {"count": len(endpoints)})

        # 2) A 模型生成功能用例（程序校验 + 兜底）
        cases = self._generate_cases(endpoints, plan, task.requirement)
        log_event(logger, "functional_cases_generated", {"count": len(cases)})

        # 3) 产物
        features = sorted({c.feature for c in cases})
        if cases:
            payload = {
                "requirement": task.requirement,
                "generator_model": getattr(self.llm, "name", "unknown"),
                "features": features,
                "cases": [c.to_dict() for c in cases],
            }
            self._write_artifacts(payload)

        summary = f"功能测试用例生成完成：{len(cases)} 条（{len(features)} 个功能点：{'、'.join(features) or '-'}）"
        log_event(logger, "functional_done", {"count": len(cases), "features": features})
        return {"summary": summary, "cases": [c.to_dict() for c in cases],
                "features": features, "count": len(cases)}

    # ---------------- 用例生成 ----------------
    def _generate_cases(self, endpoints: list[dict], plan: TestPlan, requirement: str) -> list[FunctionalCase]:
        system = (Path(__file__).parent.parent / "prompts" / "functional_tester.md").read_text(encoding="utf-8")
        user = (
            f"functional_case 任务：为以下需求与接口生成功能测试用例\n"
            f"测试需求：{requirement}\n"
            f"测试计划：{json.dumps(plan.to_dict(), ensure_ascii=False)}\n"
            f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}"
        )
        try:
            raw = self.llm.chat_json(system, user)
            raw_cases = raw.get("cases", []) if isinstance(raw, dict) else raw
        except Exception as e:  # noqa: BLE001
            logger.warning("功能用例 LLM 生成失败，使用程序兜底: %s", e)
            raw_cases = []

        cleaned = self._clean_cases(raw_cases, endpoints)

        # 兜底：LLM 没有任何合法用例时，按接口语义生成
        if not cleaned:
            cleaned = self._rule_cases(endpoints, requirement)
        return cleaned

    @staticmethod
    def _clean_cases(raw_cases: list[Any], endpoints: list[dict]) -> list[FunctionalCase]:
        """程序校验：category 合法、接口引用必须存在于文档、必备字段非空；非法丢弃。"""
        endpoint_map = {(e["method"].upper(), e["path"]): True for e in endpoints}
        valid = set(FUNCTIONAL_CATEGORIES)
        out: list[FunctionalCase] = []
        for i, c in enumerate(raw_cases):
            if not isinstance(c, dict):
                continue
            category = str(c.get("category", "normal"))
            if category not in valid:
                continue
            # 接口引用过滤：只保留文档中真实存在的端点；无合法引用则丢弃该用例
            eps = []
            for ep in c.get("involved_endpoints", []) or []:
                m = str(ep.get("method", "")).upper() if isinstance(ep, dict) else ""
                p = ep.get("path", "") if isinstance(ep, dict) else ""
                if (m, p) in endpoint_map:
                    eps.append({"method": m, "path": p})
            if not eps:
                continue
            title = str(c.get("title", "")).strip()
            expected = str(c.get("expected", "")).strip()
            if not title or not expected:
                continue
            case = FunctionalCase(
                id=str(c.get("id") or f"FC-{i + 1:03d}"),
                feature=str(c.get("feature", "未分类")).strip() or "未分类",
                title=title, category=category,
                preconditions=str(c.get("preconditions", "")),
                steps=[str(s) for s in (c.get("steps", []) or [])],
                test_data=dict(c.get("test_data", {}) or {}),
                expected=expected,
                involved_endpoints=eps,
                traceability=dict(c.get("traceability", {}) or {}),
            )
            out.append(case)
        return out

    @staticmethod
    def _rule_cases(endpoints: list[dict], requirement: str) -> list[FunctionalCase]:
        """程序兜底：按接口语义生成功能场景（登录→凭证；商品→CRUD）。"""
        cases: list[FunctionalCase] = []
        base_req = requirement[:60]

        def _find(path_sub: str, method: str) -> list[dict]:
            return [{"method": e["method"].upper(), "path": e["path"]}
                    for e in endpoints if path_sub in e["path"] and e["method"].upper() == method]

        login = _find("login", "POST")
        if login:
            cases.append(FunctionalCase(
                id="FC-001", feature="用户登录", title="正确凭证登录成功", category="normal",
                preconditions="用户已注册（admin/123456）",
                steps=["构造正确的用户名与密码", f"调用 POST {login[0]['path']}", "校验返回 token 与用户信息"],
                test_data={"username": "admin", "password": "123456"},
                expected="返回 200 且包含 token、user 字段", involved_endpoints=login,
                traceability={"requirement": base_req}))
            cases.append(FunctionalCase(
                id="FC-002", feature="用户登录", title="错误密码登录失败", category="negative",
                preconditions="用户已注册（admin/123456）",
                steps=["构造正确用户名 + 错误密码", f"调用 POST {login[0]['path']}"],
                test_data={"username": "admin", "password": "wrong"},
                expected="返回 401，不返回 token", involved_endpoints=login,
                traceability={"requirement": base_req}))

        p_post = _find("/api/v1/products", "POST")
        if p_post:
            cases.append(FunctionalCase(
                id="FC-003", feature="商品管理", title="创建合法商品成功", category="normal",
                preconditions="已登录，存在可用的商品创建接口",
                steps=["构造合法商品数据（名称+价格）", f"调用 POST {p_post[0]['path']}"],
                test_data={"name": "测试商品", "price": 99.9},
                expected="返回 201 且包含商品 id、name、price", involved_endpoints=p_post,
                traceability={"requirement": base_req}))
            cases.append(FunctionalCase(
                id="FC-004", feature="商品管理", title="价格边界创建", category="boundary",
                preconditions="接口文档 price 要求 > 0",
                steps=["价格传 0 与负数", f"调用 POST {p_post[0]['path']}"],
                test_data={"name": "边界商品", "price": 0},
                expected="返回 422，非法价格被拒绝", involved_endpoints=p_post,
                traceability={"requirement": base_req}))

        p_get = _find("/products/", "GET")
        if p_get:
            cases.append(FunctionalCase(
                id="FC-005", feature="商品管理", title="查询不存在商品", category="negative",
                preconditions="商品 id=999999 不存在",
                steps=["使用不存在的商品 id", f"调用 GET {p_get[0]['path']}"],
                test_data={"product_id": 999999},
                expected="返回 404", involved_endpoints=p_get,
                traceability={"requirement": base_req}))
        return cases

    # ---------------- 产物 ----------------
    def _write_artifacts(self, payload: dict) -> None:
        if not self.artifacts:
            return
        self.artifacts.write("functional_tester", "functional_cases", payload, "json")
        self.artifacts.write("functional_tester", "functional_cases", self._render_markdown(payload), "md")

    @staticmethod
    def _render_markdown(payload: dict) -> str:
        lines = [
            "# 功能测试用例（A 模型生成）",
            "",
            f"- 需求：{payload.get('requirement', '')[:200]}",
            f"- 生成模型：{payload.get('generator_model', '-')}",
            f"- 功能点：{'、'.join(payload.get('features', []))}",
            "",
            "| ID | 功能点 | 场景 | 类型 | 前置条件 | 步骤 | 测试数据 | 预期结果 | 涉及接口 | 需求追溯 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for c in payload.get("cases", []):
            eps = "; ".join(f"{e['method']} {e['path']}" for e in c.get("involved_endpoints", []))
            steps = "<br>".join(c.get("steps", []))
            data = json.dumps(c.get("test_data", {}), ensure_ascii=False)
            req = c.get("traceability", {}).get("requirement", "-")
            lines.append(f"| {c.get('id')} | {c.get('feature')} | {c.get('title')} | {c.get('category')} "
                         f"| {c.get('preconditions', '-')} | {steps} | {data} | {c.get('expected')} | {eps} | {req} |")
        return "\n".join(lines)

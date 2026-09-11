"""安全测试用例 Agent（SecurityTester）—— 生成侧（A 模型），自定义 Agent 接入的完整示例。

输出两类产物：
1. 安全测试用例（cases）：越权访问 / 注入 / 凭证安全 / 敏感信息 / 限流 ——
   A 模型生成 + 程序校验（category 合法、接口引用必须存在于文档、必备字段非空）
2. 静态审查结论（results）：基于 OpenAPI 的程序扫描（鉴权缺口、敏感端点暴露），
   status="failed" 的项自动进入 Verifier 失败分类链路（与可执行测试同一处理路径）

接入契约（5 处，见 registry/state_machine/artifacts/orchestrator 注释）：
- 构造签名 (llm, artifacts)；run(task, plan) -> dict
- mainline 主线 Agent：注册表 mainline=True，状态机 SECURITY_TEST，scope 白名单 + "security"
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import TestPlan, TestTask
from harness.artifacts import ArtifactManager

logger = get_logger("security_tester")

SECURITY_CATEGORIES = ("unauthorized_access", "injection", "auth_credential",
                       "sensitive_data", "rate_limit")


class SecurityTester:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts

    def run(self, task: TestTask, plan: TestPlan, spec_path: str | None = None) -> dict:
        set_agent_context("security_tester")
        spec_path = spec_path or task.meta.get("openapi_path") or str(Path(__file__).parent.parent / "examples" / "openapi_demo.yaml")

        from agents.api_tester import parse_openapi

        endpoints = parse_openapi(spec_path)
        log_event(logger, "endpoints_parsed", {"count": len(endpoints)})

        # 1) A 模型生成安全用例（程序校验 + 兜底）
        cases = self._generate_cases(endpoints, task.requirement)
        log_event(logger, "security_cases_generated", {"count": len(cases)})

        # 2) 程序静态审查（鉴权缺口扫描）→ results（failed 项进 Verifier 分类）
        results = self._scan_auth_gaps(spec_path, endpoints)

        # 3) 产物
        if cases:
            payload = {
                "requirement": task.requirement,
                "generator_model": getattr(self.llm, "name", "unknown"),
                "cases": cases,
                "auth_scan": results,
            }
            self._write_artifacts(payload)

        n_gap = sum(1 for r in results if r.get("status") == "failed")
        summary = f"安全用例生成完成：{len(cases)} 条；鉴权缺口扫描发现 {n_gap} 个风险"
        log_event(logger, "security_done", {"cases": len(cases), "auth_gaps": n_gap})
        return {"summary": summary, "cases": cases, "results": results, "count": len(cases)}

    # ---------------- 用例生成 ----------------
    def _generate_cases(self, endpoints: list[dict], requirement: str) -> list[dict]:
        system = (Path(__file__).parent.parent / "prompts" / "security_tester.md").read_text(encoding="utf-8")
        user = (
            f"security_case 任务：为以下需求与接口生成安全测试用例\n"
            f"测试需求：{requirement}\n"
            f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}"
        )
        try:
            raw = self.llm.chat_json(system, user)
            raw_cases = raw.get("cases", []) if isinstance(raw, dict) else raw
        except Exception as e:  # noqa: BLE001
            logger.warning("安全用例 LLM 生成失败，使用程序兜底: %s", e)
            raw_cases = []

        cleaned = self._clean_cases(raw_cases, endpoints)
        if not cleaned:
            cleaned = self._rule_cases(endpoints, requirement)
        return cleaned

    @staticmethod
    def _clean_cases(raw_cases: list[Any], endpoints: list[dict]) -> list[dict]:
        """程序校验：category 合法、接口引用必须存在于文档、必备字段非空。"""
        endpoint_map = {(e["method"].upper(), e["path"]): True for e in endpoints}
        out: list[dict] = []
        for i, c in enumerate(raw_cases):
            if not isinstance(c, dict):
                continue
            category = str(c.get("category", ""))
            if category not in SECURITY_CATEGORIES:
                continue
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
            out.append({
                "id": str(c.get("id") or f"SC-{i + 1:03d}"),
                "feature": str(c.get("feature", "安全")).strip() or "安全",
                "title": title, "category": category,
                "preconditions": str(c.get("preconditions", "")),
                "steps": [str(s) for s in (c.get("steps", []) or [])],
                "test_data": dict(c.get("test_data", {}) or {}),
                "expected": expected,
                "involved_endpoints": eps,
                "traceability": dict(c.get("traceability", {}) or {}),
            })
        return out

    @staticmethod
    def _rule_cases(endpoints: list[dict], requirement: str) -> list[dict]:
        """程序兜底：按接口语义生成安全用例（登录→凭证/暴力破解；商品→越权/注入；敏感查询→数据泄露）。"""
        cases: list[dict] = []

        def _find(path_sub: str, method: str) -> list[dict]:
            return [{"method": e["method"].upper(), "path": e["path"]}
                    for e in endpoints if path_sub in e["path"] and e["method"].upper() == method]

        login = _find("login", "POST")
        if login:
            cases.append({
                "id": "SC-001", "feature": "凭证安全", "title": "登录接口暴力破解防护", "category": "rate_limit",
                "preconditions": "登录接口存在", "steps": ["连续发送超过限流阈值的错误密码请求", f"调用 POST {login[0]['path']}"],
                "test_data": {"attempts": 100}, "expected": "触发限流（429/锁定），不返回 token",
                "involved_endpoints": login, "traceability": {"requirement": requirement[:60]}})
            cases.append({
                "id": "SC-002", "feature": "注入", "title": "登录参数 SQL 注入", "category": "injection",
                "preconditions": "登录接口存在", "steps": ["用户名传 ' OR '1'='1", f"调用 POST {login[0]['path']}"],
                "test_data": {"username": "' OR '1'='1", "password": "x"}, "expected": "返回 401/400，不得绕过鉴权",
                "involved_endpoints": login, "traceability": {"requirement": requirement[:60]}})

        products = _find("/products", "POST") or _find("products", "GET")
        if products:
            cases.append({
                "id": "SC-003", "feature": "越权访问", "title": "未授权访问受保护接口", "category": "unauthorized_access",
                "preconditions": "接口文档未声明匿名可访问", "steps": ["不携带任何凭证", f"调用 {products[0]['method']} {products[0]['path']}"],
                "test_data": {}, "expected": "返回 401/403，不得返回业务数据",
                "involved_endpoints": products, "traceability": {"requirement": requirement[:60]}})
            cases.append({
                "id": "SC-004", "feature": "注入", "title": "商品参数注入尝试", "category": "injection",
                "preconditions": "商品接口接受参数", "steps": ["参数携带注入 payload", f"调用 {products[0]['method']} {products[0]['path']}"],
                "test_data": {"name": "<script>alert(1)</script>"}, "expected": "返回 422 或按字符串存储，不得执行/回显",
                "involved_endpoints": products, "traceability": {"requirement": requirement[:60]}})

        sensitive = _find("user", "GET") or _find("profile", "GET")
        if sensitive:
            cases.append({
                "id": "SC-005", "feature": "敏感数据", "title": "个人信息接口敏感字段泄露", "category": "sensitive_data",
                "preconditions": "查询接口返回用户信息", "steps": ["调用接口并检查响应体", f"调用 GET {sensitive[0]['path']}"],
                "test_data": {}, "expected": "密码/令牌等敏感字段必须脱敏或不返回",
                "involved_endpoints": sensitive, "traceability": {"requirement": requirement[:60]}})
        return cases

    # ---------------- 程序静态审查（可执行、可分类） ----------------
    @staticmethod
    def _scan_auth_gaps(spec_path: str | Path, endpoints: list[dict]) -> list[dict]:
        """基于 OpenAPI 声明做鉴权缺口扫描：未声明任何鉴权机制的端点 → failed。

        与可执行用例同一 results 契约：{name, status, detail/error}，
        failed 项由 orchestrator._verify 交给 Verifier 分类。
        """
        import yaml

        spec_path = Path(spec_path)
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) if spec_path.suffix.lower() in (".yaml", ".yml") else json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return [{"name": "安全审查：鉴权缺口扫描", "status": "failed",
                     "detail": "OpenAPI 解析失败", "error": str(e)}]

        has_schemes = bool((spec.get("components") or {}).get("securitySchemes"))
        global_sec = spec.get("security")  # 全局默认鉴权
        unprotected: list[str] = []
        for path, methods in (spec.get("paths") or {}).items():
            for method, op in (methods or {}).items():
                if method.lower() not in ("get", "post", "put", "delete", "patch"):
                    continue
                # op 级 security 覆盖全局；两者都无 → 视为匿名
                op_sec = op.get("security")
                if op_sec is None:
                    if not global_sec:
                        unprotected.append(f"{method.upper()} {path}")
                elif not op_sec:
                    unprotected.append(f"{method.upper()} {path}")

        if not unprotected:
            return [{"name": "安全审查：鉴权缺口扫描", "status": "passed",
                     "detail": f"全部 {len(endpoints)} 个端点已声明鉴权机制" + (f"（securitySchemes={has_schemes}）" if has_schemes else "（security 声明）")}]
        detail = f"发现 {len(unprotected)} 个端点未声明鉴权：{', '.join(unprotected[:5])}{'…' if len(unprotected) > 5 else ''}"
        return [{"name": "安全审查：鉴权缺口扫描", "status": "failed",
                 "detail": detail, "error": "未声明鉴权（security）的端点存在匿名访问风险"}]

    # ---------------- 产物 ----------------
    def _write_artifacts(self, payload: dict) -> None:
        if not self.artifacts:
            return
        self.artifacts.write("security_tester", "security_cases", payload, "json")
        self.artifacts.write("security_tester", "security_cases", self._render_markdown(payload), "md")

    @staticmethod
    def _render_markdown(payload: dict) -> str:
        lines = [
            "# 安全测试用例（A 模型生成）",
            "",
            f"- 需求：{payload.get('requirement', '')[:200]}",
            f"- 生成模型：{payload.get('generator_model', '-')}",
            "",
            "| ID | 类别 | 场景 | 前置条件 | 步骤 | 测试数据 | 预期结果 | 涉及接口 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for c in payload.get("cases", []):
            eps = "; ".join(f"{e['method']} {e['path']}" for e in c.get("involved_endpoints", []))
            steps = "<br>".join(c.get("steps", []))
            data = json.dumps(c.get("test_data", {}), ensure_ascii=False)
            lines.append(f"| {c.get('id')} | {c.get('category')} | {c.get('title')} | {c.get('preconditions', '-')} "
                         f"| {steps} | {data} | {c.get('expected')} | {eps} |")
        lines.append("")
        lines.append("## 鉴权缺口扫描（程序静态审查）")
        for r in payload.get("auth_scan", []):
            mark = "✅" if r.get("status") == "passed" else "❌"
            lines.append(f"- {mark} {r.get('name')}：{r.get('detail', '')}")
        return "\n".join(lines)

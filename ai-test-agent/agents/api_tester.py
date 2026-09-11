"""API 测试 Agent（APITester）。

流程：
1. 解析 OpenAPI/Swagger 接口文档（程序）
2. 根据测试计划生成接口用例（LLM 生成语义、程序校验 schema）
3. 生成 pytest 风格用例代码（产物契约）
4. 执行接口调用并断言（subprocess + pytest --junitxml 解析）
5. 返回结构化结果

设计原则（对齐项目计划 §10.3/10.4）：
- 稳定断言优先：状态码必须断言、业务关键字段断言、动态字段不断言
- 程序兜底：method/path/状态码合法性由程序校验，LLM 输出超约束丢弃
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import TestPlan, TestTask
from harness.artifacts import ArtifactManager

logger = get_logger("api_tester")

DEFAULT_BASE_URL = "http://127.0.0.1:8100"

# 动态字段黑名单：这些字段不允许断言（必然误报）
_DYNAMIC_FIELDS = {"id", "token", "created_at", "updated_at", "timestamp", "ts", "uuid", "trace_id", "request_id"}


# ---------------- OpenAPI 解析（程序） ----------------
def parse_openapi(spec_path: str | Path) -> list[dict]:
    """解析 OpenAPI/Swagger 文档，提取端点列表。"""
    spec_path = Path(spec_path)
    raw = spec_path.read_text(encoding="utf-8")
    try:
        spec = yaml.safe_load(raw) if spec_path.suffix.lower() in (".yaml", ".yml") else json.loads(raw)
    except Exception as e:
        raise ValueError(f"OpenAPI 文档解析失败: {e}")

    endpoints: list[dict] = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            params = _extract_params(op)
            endpoints.append({
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "params": params,
                "responses": list((op.get("responses") or {}).keys()),
            })
    if not endpoints:
        raise ValueError("OpenAPI 文档中没有解析到可用端点")
    return endpoints


def _extract_params(op: dict) -> dict:
    """提取请求参数：body schema + path/query 参数。"""
    params: dict[str, Any] = {"path": [], "query": [], "body": None}
    for p in op.get("parameters") or []:
        target = params["path"] if p.get("in") == "path" else params["query"]
        target.append({"name": p.get("name"), "required": p.get("required", False), "type": p.get("schema", {}).get("type", "string")})
    body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
    if body:
        props = body.get("properties", {})
        required = set(body.get("required", []))
        params["body"] = {
            "required": sorted(required),
            "properties": {k: {"type": v.get("type", "string"), "required": k in required,
                               "maxLength": v.get("maxLength"), "minimum": v.get("minimum")}
                           for k, v in props.items()},
        }
    return params


# ---------------- Agent ----------------
class APITester:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts

    def run(self, task: TestTask, plan: TestPlan, spec_path: str | None = None, base_url: str | None = None,
            rag_context: str = "") -> dict:
        set_agent_context("api_tester")
        base_url = base_url or task.meta.get("base_url") or DEFAULT_BASE_URL
        spec_path = spec_path or task.meta.get("openapi_path") or str(Path(__file__).parent.parent / "examples" / "openapi_demo.yaml")

        # 1) 接口解析（程序）
        endpoints = parse_openapi(spec_path)
        log_event(logger, "endpoints_parsed", {"count": len(endpoints), "paths": [e["path"] for e in endpoints][:8]})

        # 2) 用例生成（B12：知识库三类知识注入 + LLM + 程序校验）
        cases = self._generate_cases(endpoints, plan, task.requirement, rag_context=rag_context)
        log_event(logger, "cases_generated", {"count": len(cases)})

        # 3) 产物：生成 pytest 代码
        code = self._render_pytest(cases, base_url)
        if self.artifacts:
            self.artifacts.write("api_tester", "api_test_cases", code, "code")

        # 4) 执行
        results = self._execute(code, base_url, task.task_id)
        passed = sum(1 for r in results if r["status"] == "passed")
        summary = f"API 测试完成：{passed}/{len(results)} 通过（base_url={base_url}）"
        log_event(logger, "api_done", {"passed": passed, "total": len(results)})
        return {"summary": summary, "results": results, "base_url": base_url, "cases": cases}

    # ---------------- 用例生成 ----------------
    def _generate_cases(self, endpoints: list[dict], plan: TestPlan, requirement: str,
                        rag_context: str = "") -> list[dict]:
        system = (Path(__file__).parent.parent / "prompts" / "api_tester.md").read_text(encoding="utf-8")
        user = (
            f"api_case 任务：为以下接口生成测试用例\n"
            f"测试需求：{requirement}\n"
            f"测试计划：{json.dumps(plan.to_dict(), ensure_ascii=False)}\n"
            f"接口清单：{json.dumps(endpoints, ensure_ascii=False)}"
        )
        # B12：知识库三类知识（操作手册/基线用例/上线检查清单）注入，附来源引用
        if rag_context:
            user += f"\n【知识库参考上下文（严格参考，来源可追溯）】\n{rag_context}"
        try:
            raw = self.llm.chat_json(system, user)
            cases = raw if isinstance(raw, list) else raw.get("cases", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 用例生成失败，使用程序兜底: %s", e)
            cases = []

        # 程序校验：method/path 合法、category 合法；非法丢弃
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        valid_categories = {"normal", "negative", "boundary"}
        endpoint_map = {(e["method"], e["path"]): e for e in endpoints}
        cleaned: list[dict] = []
        for c in cases:
            m, p = str(c.get("method", "")).upper(), c.get("path", "")
            if m not in valid_methods or (m, p) not in endpoint_map:
                continue
            c["method"], c["path"] = m, p
            c["category"] = c.get("category", "normal") if c.get("category") in valid_categories else "normal"
            # 断言字段过滤：去掉动态字段
            c["expect_fields"] = [f for f in c.get("expect_fields", []) if f not in _DYNAMIC_FIELDS]
            cleaned.append(c)

        # 兜底：LLM 没生成任何合法用例时，为前 3 个端点生成程序用例
        if not cleaned:
            for ep in endpoints[:3]:
                cleaned.extend(self._rule_cases(ep))
        return cleaned

    def _rule_cases(self, ep: dict) -> list[dict]:
        """程序兜底用例（正常/异常/边界）。"""
        body = ep.get("params", {}).get("body")
        path = ep["path"]
        m = ep["method"]
        stem = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        # 按接口语义生成参数（登录→凭证，商品→CRUD 字段）
        if "login" in path:
            good = {"username": "admin", "password": "123456"}
            fields = ["token", "user"]
            boundary = {"username": "x" * 200, "password": "123456"}
            boundary_status = 401
        else:
            good = {"name": "测试商品", "price": 99.9}
            fields = ["id", "name", "price"]
            boundary = {k: ("x" * 200 if isinstance(v, str) else v) for k, v in good.items()}
            boundary_status = 422
        # path 参数（如 /products/{product_id}）用存在的 id
        path_params = {k: 1001 for k in re.findall(r"\{(\w+)\}", path)}
        expect = 200 if "login" in path else (201 if m == "POST" else 200)
        cases = [{
            "name": f"test_{m.lower()}_{stem}_normal", "method": m, "path": path,
            "category": "normal", "params": {**good, **path_params},
            "expect_status": expect, "expect_fields": fields,
        }]
        if m in ("POST", "PUT"):
            cases.append({
                "name": f"test_{m.lower()}_{stem}_missing_field", "method": m, "path": path,
                "category": "negative", "params": {},
                "expect_status": 422, "expect_fields": [],
            })
            cases.append({
                "name": f"test_{m.lower()}_{stem}_boundary", "method": m, "path": path,
                "category": "boundary", "params": boundary,
                "expect_status": boundary_status, "expect_fields": [],
            })
        return cases

    # ---------------- pytest 渲染 ----------------
    @staticmethod
    def _render_pytest(cases: list[dict], base_url: str) -> str:
        lines = [
            '"""AI 生成的 API 测试用例（pytest 风格）—— 请勿手改，重新生成会覆盖。"""',
            "import requests",
            "import pytest",
            "",
            f'BASE_URL = "{base_url}"',
            "",
        ]
        for i, c in enumerate(cases):
            # A17 结构化映射 + 双向追溯：每个用例 = 一个编号步骤块（Step {case}.{step}）
            step_key = f"{i + 1}.1"
            lines.append(f"# Step {step_key}: {c['name']}（{c['method']} {c['path']}，期望 {c.get('expect_status')}）")
            lines.append(f"def test_{i}_{_safe_name(c['name'])}():")
            path = _render_path(c["path"], c.get("params", {}))
            lines.append(f"    resp = requests.{c['method'].lower()}(")
            lines.append(f'        f"{{BASE_URL}}{path}",')
            if c["method"] in ("POST", "PUT") and c.get("params"):
                lines.append(f"        json={json.dumps(c['params'], ensure_ascii=False)},")
            lines.append("        timeout=10,")
            lines.append("    )")
            lines.append(f"    assert resp.status_code == {c['expect_status']}, f\"期望 {c['expect_status']} 实际 {{resp.status_code}}: {{resp.text[:200]}}\"")
            for f in c.get("expect_fields", []):
                lines.append(f"    assert '{f}' in resp.json(), f\"缺少字段 {{'{f}'}}\"")
            lines.append("")
        return "\n".join(lines)

    # ---------------- 执行 ----------------
    def _execute(self, code: str, base_url: str, task_id: str) -> list[dict]:
        if not self._probe(base_url):
            return [{
                "name": "环境检查", "status": "error",
                "error": f"被测服务不可达: {base_url}（请先启动 python -m uvicorn examples.demo_api:app --port 8100）",
            }]
        # 落盘临时 pytest 文件并执行
        tmp_dir = self.artifacts.run_dir / "api_tester" if self.artifacts else Path("/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        py_file = tmp_dir / f"run_{task_id[-6:]}.py"
        py_file.write_text(code, encoding="utf-8")
        xml_file = tmp_dir / f"junit_{task_id[-6:]}.xml"
        cmd = ["python3", "-m", "pytest", str(py_file), f"--junitxml={xml_file}", "-q", "--tb=no", "-p", "no:cacheprovider"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        results = self._parse_junit(xml_file)
        if not results:  # junit 没生成（如 pytest 崩溃），回退到 stdout 摘要
            results = [{"name": "pytest_run", "status": "error", "error": proc.stdout[-500:] or proc.stderr[-500:]}]
        return results

    @staticmethod
    def _probe(base_url: str) -> bool:
        import requests

        try:
            r = requests.get(f"{base_url}/healthz", timeout=3)
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _parse_junit(xml_file: Path) -> list[dict]:
        if not xml_file.exists():
            return []
        tree = ET.parse(xml_file)
        results = []
        for tc in tree.iter("testcase"):
            duration = float(tc.get("time", "0")) * 1000
            failure = tc.find("failure")
            if failure is not None:
                results.append({"name": tc.get("name", ""), "status": "failed",
                                "duration_ms": duration, "error": (failure.get("message") or failure.text or "")[:500]})
            else:
                results.append({"name": tc.get("name", ""), "status": "passed", "duration_ms": duration})
        return results


def _safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)[:80]


def _render_path(path: str, params: dict) -> str:
    """把 path 中的 {param} 占位符替换为用例参数值（或默认值），避免渲染成 Python 变量。"""
    def _repl(m):
        key = m.group(1)
        val = params.get(key, "1")
        return str(val)

    return re.sub(r"\{(\w+)\}", _repl, path)

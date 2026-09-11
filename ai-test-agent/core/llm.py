"""LLM 接入抽象层。

统一接口：
    llm.chat(system, user, json_mode=False) -> str | dict

实现：
- OpenAICompatibleLLM：OpenAI 兼容协议（火山引擎豆包 / OpenAI / 任意兼容网关）
- MockLLM：未配置 API Key 时的确定性规则引擎，保证全流程可运行、可演示

设计原则（对齐项目计划 §10.3）：
- LLM 只做语义理解，字段校验 / 值匹配 / schema 校验全部由程序完成
- 输出受 schema 约束（json_mode 强制 JSON，超出约束由调用方丢弃）
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import requests

from .config import get_settings
from .logging_setup import get_logger

logger = get_logger("llm")


class LLMError(Exception):
    pass


class BaseLLM:
    name: str = "base"

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str | dict:
        raise NotImplementedError

    # ---- 便捷方法 ----
    def chat_text(self, system: str, user: str, **kwargs: Any) -> str:
        out = self.chat(system, user, json_mode=False, **kwargs)
        return str(out)

    def chat_json(self, system: str, user: str, **kwargs: Any) -> dict:
        out = self.chat(system, user, json_mode=True, **kwargs)
        if isinstance(out, dict):
            return out
        raise LLMError(f"期望 JSON 输出，实际得到文本: {str(out)[:200]}")


class OpenAICompatibleLLM(BaseLLM):
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int, max_retries: int, temperature: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str | dict:
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": kwargs.get("temperature", self.temperature),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if json_mode:
                    return self._parse_json(content)
                return content
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM 调用失败(第%s次): %s", attempt + 1, e)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"LLM 调用最终失败: {last_err}")

    @staticmethod
    def _parse_json(content: str) -> dict:
        text = content.strip()
        # 容忍 ```json 包裹
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            text = m.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 取第一个 { 到最后一个 }
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise


class MockLLM(BaseLLM):
    """确定性 mock：无 API Key 时让整个流水线可跑通、可演示。

    通过关键词路由到对应场景的规则生成器，输出与真实 LLM 同构。
    """

    name = "mock"

    def __init__(self) -> None:
        self._call_count = 0

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs: Any) -> str | dict:
        self._call_count += 1
        text = f"{system}\n{user}"
        handler = self._route(text)
        result = handler(system, user)
        if json_mode:
            if isinstance(result, dict):
                return result
            return {"text": str(result)}
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    # ---- 路由 ----
    def _route(self, text: str):
        # 具体场景路由优先（各 Agent 显式携带英文标记），"制定测试计划" 仅匹配总控 Orchestrator 的 system
        routes = [
            ("functional_review", self._functional_review),
            ("functional_case", self._functional_cases),
            ("requirement_parse", self._requirement_parse),
            ("ui_script", self._ui_script),
            ("api_case", self._api_cases),
            ("code_review", self._code_review),
            ("unit_test", self._unit_test),
            ("failure_classify", self._failure_classify),
            ("report_summary", self._report_summary),
            ("requirement", self._requirement),
            ("制定测试计划", self._plan),
        ]
        for key, fn in routes:
            if key in text:
                return fn
        return self._generic

    # ---- 各场景生成器 ----
    @staticmethod
    def _requirement_parse(system: str, user: str) -> dict:
        """需求三级解析（mock 兜底）：按关键词识别功能点，接口引用对齐接口清单。"""
        eps = MockLLM._extract_endpoints(user)
        ep_map = {(e["method"].upper(), e["path"]): e for e in eps}
        text = user
        items: list[dict] = []
        n = 0

        def _ep(*patterns: str) -> list[dict]:
            out = []
            for (m, p), e in ep_map.items():
                if any(k in p for k in patterns):
                    out.append({"method": m, "path": p})
            return out

        def _add(title: str, desc: str, acceptance: list[str], rules: list[str],
                 constraints: list[str], priority: str, src: str, patterns: tuple[str, ...]) -> None:
            nonlocal n
            n += 1
            items.append({
                "id": f"RQ-{n:03d}", "title": title, "desc": desc, "acceptance": acceptance,
                "rules": rules, "constraints": constraints, "priority": priority,
                "involved_endpoints": _ep(*patterns), "source": src,
            })

        if any(k in text for k in ("登录", "认证", "凭证")):
            _add("用户登录", "用户使用用户名密码登录获取凭证",
                 ["正确凭证返回 200 且包含 token", "错误密码返回 401 且不返回 token"],
                 ["登录失败不得发放凭证", "密码为敏感信息，比较需常量时间"],
                 ["用户名超长（>200 字符）应被拒绝或明确报错"], "P0", "3.2 用户登录",
                 ("login",))
        if any(k in text for k in ("注册", "开户", "新增用户")):
            _add("用户注册", "新用户注册",
                 ["合法用户名密码注册成功", "重复用户名注册报错"],
                 ["用户名密码不能为空", "用户名唯一"], [], "P1", "3.1 用户注册", ("register", "user"))
        if any(k in text for k in ("商品", "创建", "发布", "新增")):
            _add("商品创建", "创建商品",
                 ["合法商品数据返回 201 且含 id/name/price", "缺少必填字段返回 422"],
                 ["价格必须大于 0", "名称长度不超过 100"],
                 ["价格边界（0/负数）应被拒绝"], "P0", "4.1 商品发布",
                 ("products",))
        if any(k in text for k in ("查询", "列表", "详情")):
            _add("商品查询", "按 id 查询商品",
                 ["存在的商品返回 200 且含商品信息", "不存在的商品返回 404"],
                 [], [], "P1", "4.2 商品查询", ("products",))
        if any(k in text for k in ("权限", "未授权", "越权", "鉴权")):
            _add("权限校验", "未授权访问被拒绝",
                 ["未携带有效凭证访问受保护接口返回 401/403"],
                 ["受保护接口必须校验凭证"], ["权限校验不允许绕过"], "P0", "5 权限模型",
                 ("login", "products"))
        if not items:
            items.append({
                "id": "RQ-001", "title": "核心业务主流程", "desc": text.strip().splitlines()[0][:80],
                "acceptance": ["主流程可正常走通", "异常输入可被正确处理"],
                "rules": [], "constraints": [], "priority": "P1",
                "involved_endpoints": [{"method": e["method"].upper(), "path": e["path"]} for e in eps[:2]],
                "source": "需求文档",
            })
        return {"summary": text.strip().splitlines()[0][:80], "risks": ["关键链路不可用", "数据不一致"],
                "items": items}

    @staticmethod
    def _functional_cases(system: str, user: str) -> dict:
        """功能测试用例（mock 兜底）：按接口语义推导业务功能场景（登录/商品 CRUD）。"""
        eps = MockLLM._extract_endpoints(user)
        cases: list[dict] = []
        n = 0

        def _add(feature: str, title: str, category: str, preconditions: str, steps: list[str],
                 data: dict, expected: str, endpoints: list[dict], req: str = "") -> None:
            nonlocal n
            n += 1
            cases.append({
                "id": f"FC-{n:03d}", "feature": feature, "title": title, "category": category,
                "preconditions": preconditions, "steps": steps, "test_data": data,
                "expected": expected, "involved_endpoints": endpoints,
                "traceability": {"requirement": req or feature},
            })

        login = [e for e in eps if "login" in e["path"] and e["method"] == "POST"]
        product_post = [e for e in eps if e["path"] == "/api/v1/products" and e["method"] == "POST"]
        product_get = [e for e in eps if "/products/" in e["path"] and e["method"] == "GET"]
        product_put = [e for e in eps if "/products/" in e["path"] and e["method"] == "PUT"]
        base_req = "商品发布与登录认证功能"

        # 登录
        if login:
            _add("用户登录", "正确凭证登录成功", "normal", "用户已注册（admin/123456）",
                 ["构造正确的用户名与密码", "调用 POST /api/v1/login", "校验返回 token 与用户信息"],
                 {"username": "admin", "password": "123456"}, "返回 200 且包含 token、user 字段", login, base_req)
            _add("用户登录", "错误密码登录失败", "negative", "用户已注册（admin/123456）",
                 ["构造正确用户名 + 错误密码", "调用 POST /api/v1/login"],
                 {"username": "admin", "password": "wrong"}, "返回 401，不返回 token", login, base_req)
            _add("用户登录", "超长用户名登录", "boundary", "接口文档 username 为 string 无长度上限",
                 ["构造 200 字符用户名", "调用 POST /api/v1/login"],
                 {"username": "x" * 200, "password": "123456"}, "返回 401 或 422，系统不崩溃", login, base_req)

        # 商品创建
        if product_post:
            _add("商品管理", "创建合法商品成功", "normal", "已登录，存在可用的商品创建接口",
                 ["构造合法商品数据（名称+价格）", "调用 POST /api/v1/products"],
                 {"name": "测试商品", "price": 99.9}, "返回 201 且包含商品 id、name、price", product_post, base_req)
            _add("商品管理", "缺少必填字段创建失败", "negative", "已登录",
                 ["只传价格不传名称", "调用 POST /api/v1/products"],
                 {"price": 99.9}, "返回 422 并给出字段校验错误", product_post, base_req)
            _add("商品管理", "价格边界创建", "boundary", "接口文档 price 要求 > 0",
                 ["价格传 0 与负数", "调用 POST /api/v1/products"],
                 {"name": "边界商品", "price": 0}, "返回 422，非法价格被拒绝", product_post, base_req)

        # 商品查询
        if product_get:
            _add("商品管理", "查询存在商品成功", "normal", "存在商品 id=1001",
                 ["使用存在的商品 id", "调用 GET /api/v1/products/{product_id}"],
                 {"product_id": 1001}, "返回 200 且包含商品信息", product_get, base_req)
            _add("商品管理", "查询不存在商品", "negative", "商品 id=999999 不存在",
                 ["使用不存在的商品 id", "调用 GET /api/v1/products/{product_id}"],
                 {"product_id": 999999}, "返回 404", product_get, base_req)

        # 商品更新
        if product_put:
            _add("商品管理", "更新商品成功", "normal", "存在商品 id=1001",
                 ["构造合法更新数据", "调用 PUT /api/v1/products/{product_id}"],
                 {"product_id": 1001, "name": "更新商品", "price": 88.0}, "返回 200 且字段更新生效", product_put, base_req)

        return {"cases": cases}

    @staticmethod
    def _functional_review(system: str, user: str) -> dict:
        """功能用例评审（mock 兜底）：程序规则检查 + 语义建议。

        硬性规则（接口引用合法性、必备字段）由 functional_reviewer 的程序层做；
        这里做轻量规则：用例缺少预期结果 / 未引用任何接口 → 记为 fail。
        """
        cases = MockLLM._extract_review_cases(user)
        findings: list[dict] = []
        missing: list[str] = []
        valid_cats = {"normal", "negative", "boundary", "security"}
        for c in cases:
            cid = str(c.get("id", "?"))
            if not str(c.get("expected", "")).strip():
                findings.append({"case_id": cid, "verdict": "fail", "severity": "high",
                                 "issue": "用例缺少预期结果描述，无法判定是否符合需求",
                                 "suggestion": "补充明确的预期结果（状态码/字段/业务状态）"})
            if not (c.get("involved_endpoints") or []):
                findings.append({"case_id": cid, "verdict": "fail", "severity": "high",
                                 "issue": "用例未引用任何接口端点，无法与接口文档对齐",
                                 "suggestion": "在 involved_endpoints 中补充方法+路径"})
            if str(c.get("category", "")) not in valid_cats:
                findings.append({"case_id": cid, "verdict": "fail", "severity": "medium",
                                 "issue": f"category 非法: {c.get('category')}",
                                 "suggestion": "使用 normal/negative/boundary/security"})
        if not cases:
            missing.append("未生成任何可解析的功能用例，需重新生成")
        if not findings:
            findings.append({"case_id": "-", "verdict": "pass", "severity": "low",
                             "issue": "用例结构与接口文档对齐，未发现明显不符合项",
                             "suggestion": "建议补充安全类场景（未授权访问）与并发场景"})
        return {"overall": "issues_found" if any(f["verdict"] != "pass" for f in findings) else "pass",
                "summary": f"评审完成：{len(findings)} 条评审意见，{len(missing)} 条缺失场景提示",
                "findings": findings, "missing_scenarios": missing}

    @staticmethod
    def _extract_review_cases(user: str) -> list[dict]:
        """从"待评审用例："标记后提取用例 JSON（list 或 {"cases": [...]}）。"""
        import json as _json
        idx = user.find("待评审用例：")
        if idx == -1:
            return []
        start = user.find("[", idx)
        if start == -1:
            start = user.find("{", idx)
        if start == -1:
            return []
        try:
            obj, _ = _json.JSONDecoder().raw_decode(user[start:])
        except Exception:  # noqa: BLE001
            return []
        if isinstance(obj, list):
            return [c for c in obj if isinstance(c, dict)]
        if isinstance(obj, dict):
            return [c for c in obj.get("cases", []) if isinstance(c, dict)]
        return []

    @staticmethod
    def _extract_endpoints(user: str) -> list[dict]:
        """从 user 中提取"接口清单：{json}"（与 api_tester/functional_tester 输入格式一致）。"""
        import json as _json
        idx = user.find("接口清单：")
        if idx == -1:
            return []
        start = user.find("[", idx)
        if start == -1:
            start = user.find("{", idx)
        if start == -1:
            return []
        try:
            obj, _ = _json.JSONDecoder().raw_decode(user[start:])
        except Exception:  # noqa: BLE001
            return []
        if isinstance(obj, list):
            return obj
        return obj.get("endpoints", []) if isinstance(obj, dict) else []

    @staticmethod
    def _plan(system: str, user: str) -> dict:
        scope = ["api", "ui", "whitebox"]
        if any(k in user for k in ("功能测试", "功能回归", "业务场景", "功能点", "权限场景")):
            scope.append("functional")
        return {
            "scope": scope,
            "strategy": "正常/异常/边界 + 变更增量 + 功能场景",
            "risk_points": ["核心链路回归", "变更影响面", "需求覆盖度"],
            "scenarios": [
                {"name": "主流程", "type": "positive", "desc": "验证核心业务主链路"},
                {"name": "异常流程", "type": "negative", "desc": "参数缺失/非法输入"},
                {"name": "边界流程", "type": "boundary", "desc": "边界值与临界状态"},
                {"name": "功能场景", "type": "functional", "desc": "业务功能点与需求符合性（A生成/B评审）"},
            ],
            "estimates": {"api_cases": 6, "ui_cases": 3, "functional_cases": 6, "whitebox": "按变更范围"},
        }

    @staticmethod
    def _requirement(system: str, user: str) -> dict:
        return {
            "summary": user.strip().splitlines()[0][:60] if user.strip() else "测试需求",
            "features": ["登录鉴权", "核心业务操作", "结果校验"],
            "acceptance": ["功能正确", "异常可控", "性能达标"],
            "risk": ["关键链路不可用", "数据不一致"],
        }

    @staticmethod
    def _api_cases(system: str, user: str) -> list[dict]:
        """按接口语义生成用例（mock 兜底）：login→凭证、products→CRUD、状态码按方法区分。"""
        ep = re.search(r"(?:endpoint|接口)[：:\s]*(/\S+)", user)
        method = re.search(r"(?:method|方法)[：:\s]*(\w+)", user)
        path = ep.group(1) if ep else "/api/v1/products"
        m = method.group(1).upper() if method else ("POST" if "products" in path and "{" not in path else "GET")
        stem = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        expect = 200 if "login" in path else (201 if m == "POST" else 200)
        if "login" in path:
            good_params = {"username": "admin", "password": "123456"}
            fields = ["token", "user"]
            boundary_params = {"username": "x" * 200, "password": "123456"}
            boundary_status = 401
        else:
            good_params = {"name": "测试商品", "price": 99.9}
            fields = ["id", "name", "price"]
            boundary_params = {k: ("x" * 200 if isinstance(v, str) else v) for k, v in good_params.items()}
            boundary_status = 422
        # path 参数（如 /products/{product_id}）用存在的 id
        path_params = {k: 1001 for k in re.findall(r"\{(\w+)\}", path)}
        cases = [
            {"name": f"test_{m.lower()}_{stem}_normal",
             "method": m, "path": path, "category": "normal",
             "params": {**good_params, **path_params}, "expect_status": expect, "expect_fields": fields},
            {"name": f"test_{m.lower()}_{stem}_missing_field",
             "method": m, "path": path, "category": "negative",
             "params": {}, "expect_status": 422, "expect_fields": []},
            {"name": f"test_{m.lower()}_{stem}_boundary",
             "method": m, "path": path, "category": "boundary",
             "params": boundary_params, "expect_status": boundary_status, "expect_fields": []},
        ]
        return cases
        return cases

    @staticmethod
    def _ui_script(system: str, user: str) -> str:
        url = re.search(r"(?:url|地址)[：:\s]*(\S+)", user)
        base = url.group(1) if url else "http://127.0.0.1:8080/index.html"
        # 与 ui_service 执行引擎兼容的步骤脚本（无 import / 无 async 包裹）
        return f"""// AI 生成的 UI 测试脚本（mock 模式）—— ui_service 执行引擎兼容
await agent.aiNavigate('{base}');
await agent.aiWaitFor('AI 测试智能体');
await agent.aiInput('用户名', 'admin');
await agent.aiInput('密码', '123456');
await agent.aiTap('登录');
await agent.aiWaitFor('欢迎，admin');
await agent.aiInput('商品名称', '测试商品');
await agent.aiInput('商品价格', '99.9');
await agent.aiTap('新增商品');
await agent.aiWaitFor('商品创建成功');
await agent.aiAssert('商品创建成功');
await agent.aiAssert('测试商品');"""

    @staticmethod
    def _code_review(system: str, user: str) -> dict:
        # 简单规则：从代码中识别明显问题特征（与 chat_json 的 dict 契约一致）
        findings: list[dict] = []
        code = user
        if "==" in code and "hmac" not in code:
            findings.append({"severity": "严重", "location": "疑似密码/敏感比较处",
                             "issue": "检测到直接相等比较，敏感信息比较建议使用常量时间比较", "suggestion": "使用 hmac.compare_digest()"})
        if "print(" in code:
            findings.append({"severity": "警告", "location": "函数体内",
                             "issue": "存在 print 调试输出，建议使用日志框架", "suggestion": "替换为 logging"})
        if "except" in code and "pass" in code:
            findings.append({"severity": "警告", "location": "异常处理",
                             "issue": "捕获异常后直接 pass，吞掉了错误信息", "suggestion": "记录日志并抛出可追踪异常"})
        if not findings:
            findings.append({"severity": "建议", "location": "整体",
                             "issue": "未发现明显问题，建议补充边界场景测试", "suggestion": "覆盖空输入/超长输入/并发场景"})
        return {"findings": findings}

    @staticmethod
    def _unit_test(system: str, user: str) -> dict:
        # 从 user 中提取被测函数名（prompt 内 JSON payload 的 function 字段）
        funcs = re.findall(r'"function":\s*"(\w+)"', user)
        if not funcs:
            funcs = re.findall(r"(?:function|def|func)\s+(\w+)\s*\(", user)
        funcs = list(dict.fromkeys(funcs)) or ["target_function"]
        lang = re.search(r"语言[：:]\s*(\w+)", user)
        lang = lang.group(1).lower() if lang else "python"
        tests = []
        if lang == "go":
            # Go：testing 包模板。引用被测函数名以做类型检查；被测源码由
            # test_generator 拷入执行目录，go test 会真实编译变更代码。
            pkg = re.search(r'\\npackage\s+(\w+)', user) or re.search(r"package\s+(\w+)", user)
            pkg = pkg.group(1) if pkg else "main"
            for name in funcs:
                tests.append({
                    "function": name,
                    "code": f'''package {pkg}

import "testing"

// AI 生成的单测（mock 模式）：{name}
// 被测源码已由平台拷入同包，此处通过引用触发真实类型检查。
func Test{name.title()}Normal(t *testing.T) {{
    _ = {name} // 编译期校验变更函数签名与包内可见性
}}

func Test{name.title()}Boundary(t *testing.T) {{
    // 边界路径：mock 占位，真实断言由 LLM 模式生成
    _ = {name}
}}
''',
                })
            return {"tests": tests}
        if lang in ("javascript", "typescript"):
            for name in funcs:
                tests.append({
                    "function": name,
                    "code": f'''// AI 生成的单测（mock 模式）：{name}
const test = require('node:test');
const assert = require('node:assert');

test('{name} normal', () => {{
  assert.ok(true); // 真实断言由 LLM 模式生成
}});

test('{name} boundary', () => {{
  assert.ok(true);
}});
''',
                })
            return {"tests": tests}
        for name in funcs:
            tests.append({
                "function": name,
                "code": f'''"""AI 生成的单测（mock 模式）：{name}"""
import pytest


def test_{name}_normal():
    """正常路径：验证函数可调用且返回结构稳定。"""
    assert True


def test_{name}_boundary():
    """边界路径：mock 占位，真实断言由 LLM 模式生成。"""
    assert True
''',
            })
        return {"tests": tests}

    @staticmethod
    def _failure_classify(system: str, user: str) -> dict:
        text = user.lower()
        if "timeout" in text or "超时" in text:
            return {"category": "脚本错误-超时", "level": "flaky",
                    "reason": "操作超时，可能是环境慢或等待不足", "action": "增加等待重试"}
        if "assert" in text or "断言" in text or "expected" in text:
            return {"category": "真实Bug-断言失败", "level": "bug",
                    "reason": "预期结果与实际结果不一致", "action": "人工确认是否业务缺陷"}
        if "500" in text or "server error" in text:
            return {"category": "真实Bug-接口报错", "level": "bug",
                    "reason": "服务端返回 5xx", "action": "记录 Bug 并通知后端"}
        if "not found" in text or "找不到" in text or "selector" in text:
            return {"category": "脚本错误-定位失败", "level": "flaky",
                    "reason": "元素定位失败，可能页面结构变化", "action": "调整定位描述重试"}
        return {"category": "待确认", "level": "unknown",
                "reason": "无法自动判断，需要人工复核", "action": "人工复核"}

    @staticmethod
    def _report_summary(system: str, user: str) -> str:
        return "测试执行完成。总体通过率良好，发现少量失败用例，已按脚本错误/真实Bug分类，详见报告。"

    @staticmethod
    def _generic(system: str, user: str) -> str:
        return f"（mock 模式）收到请求：{user[:80]}…"


def create_llm(role: str = "main") -> BaseLLM:
    """创建 LLM 实例。

    role="main"   -> A 模型：需求解析 / 用例生成 / 执行类 Agent
    role="review" -> B 模型：独立评审（优先 AI_TEST_REVIEW_*，未配置时回退主模型）

    双模型设计（A 生成 / B 评审）：B 与 A 解耦，避免"自产自审"的同源偏见；
    未配置任何 Key 时进入 mock 模式（确定性生成，全流程可跑通）。
    """
    s = get_settings()

    if role == "review":
        if s.review_available:
            logger.info("评审使用独立 B 模型: %s @ %s", s.review_model, s.review_base_url)
            return OpenAICompatibleLLM(
                base_url=s.review_base_url, api_key=s.review_api_key, model=s.review_model,
                timeout=s.review_timeout, max_retries=s.review_max_retries, temperature=s.review_temperature,
            )
        if s.llm_available:
            logger.warning("未配置 AI_TEST_REVIEW_*（B 模型），评审回退到主模型 %s（A/B 同模型）", s.llm_model)
        else:
            logger.warning("未配置任何 API Key，评审进入 mock 模式")
        # 回退：主模型或 mock
        role = "main"

    if s.llm_available:
        logger.info("使用真实 LLM: %s @ %s", s.llm_model, s.llm_base_url)
        return OpenAICompatibleLLM(
            base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model,
            timeout=s.llm_timeout, max_retries=s.llm_max_retries, temperature=s.llm_temperature,
        )
    logger.warning("未配置 AI_TEST_LLM_API_KEY，启用 mock 模式（确定性生成，可跑通全流程）")
    return MockLLM()

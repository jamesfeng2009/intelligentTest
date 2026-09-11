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
    def _plan(system: str, user: str) -> dict:
        return {
            "scope": ["api", "ui", "whitebox"],
            "strategy": "正常/异常/边界 + 变更增量",
            "risk_points": ["核心链路回归", "变更影响面"],
            "scenarios": [
                {"name": "主流程", "type": "positive", "desc": "验证核心业务主链路"},
                {"name": "异常流程", "type": "negative", "desc": "参数缺失/非法输入"},
                {"name": "边界流程", "type": "boundary", "desc": "边界值与临界状态"},
            ],
            "estimates": {"api_cases": 6, "ui_cases": 3, "whitebox": "按变更范围"},
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


def create_llm() -> BaseLLM:
    s = get_settings()
    if s.llm_available:
        logger.info("使用真实 LLM: %s @ %s", s.llm_model, s.llm_base_url)
        return OpenAICompatibleLLM(
            base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model,
            timeout=s.llm_timeout, max_retries=s.llm_max_retries, temperature=s.llm_temperature,
        )
    logger.warning("未配置 AI_TEST_LLM_API_KEY，启用 mock 模式（确定性生成，可跑通全流程）")
    return MockLLM()

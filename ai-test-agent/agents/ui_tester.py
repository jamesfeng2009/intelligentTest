"""UI 测试 Agent（UITester）。

流程：
1. 根据测试计划生成 Midscene UI 脚本（LLM；定位描述公式：容器上下文+视觉位置+文案+元素类型）
2. 提交到独立 UI 执行服务（Node.js + Midscene + Playwright）
3. 错误自愈：定位失败/超时自动重试（≤2 次），仍失败则标记真实失败
4. 保存脚本与截图（产物契约）
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import requests

from core.config import get_settings
from core.llm import BaseLLM
from core.logging_setup import get_logger, log_event, set_agent_context
from core.models import TestPlan, TestTask
from harness.artifacts import ArtifactManager

logger = get_logger("ui_tester")

MAX_RETRY = 2


class UITester:
    def __init__(self, llm: BaseLLM, artifacts: ArtifactManager | None = None) -> None:
        self.llm = llm
        self.artifacts = artifacts
        self.settings = get_settings()

    def run(self, task: TestTask, plan: TestPlan, base_url: str | None = None) -> dict:
        set_agent_context("ui_tester")
        base_url = base_url or task.meta.get("ui_base_url") or "https://demo.example.com"

        # 1) 生成脚本
        script = self._generate_script(plan, task.requirement, base_url)
        if self.artifacts:
            self.artifacts.write("ui_tester", "ui_test_cases", script, "ts")

        # 2) 执行（带自愈重试）
        result = self._execute_with_retry(task.task_id, script, base_url)
        log_event(logger, "ui_done", {"status": result["status"], "mode": result.get("mode")})

        # 3) 保存截图
        for i, shot_b64 in enumerate(result.get("screenshots", []) or []):
            if self.artifacts and shot_b64:
                try:
                    binary = base64.b64decode(shot_b64)
                    self.artifacts.save_screenshot("ui_tester", f"screenshot_{i}", binary)
                except Exception:  # noqa: BLE001
                    logger.warning("截图保存失败（可能为 base64 编码异常）")

        results = result.get("results") or []
        return {
            "summary": f"UI 测试完成：{result.get('status')}（模式={result.get('mode', 'n/a')}）",
            "results": results,
            "script": script,
            "mode": result.get("mode"),
        }

    # ---------------- 用例生成 ----------------
    def _generate_script(self, plan: TestPlan, requirement: str, base_url: str) -> str:
        system = (Path(__file__).parent.parent / "prompts" / "ui_tester.md").read_text(encoding="utf-8")
        user = (
            f"ui_script 任务：为以下 UI 测试需求生成脚本\n"
            f"测试需求：{requirement}\n"
            f"目标地址：{base_url}\n"
            f"测试计划：{json.dumps(plan.to_dict(), ensure_ascii=False)}\n"
            "输出要求：只输出可执行脚本主体（不含 import，不含 async 包裹，agent 已注入），每行一个 await agent.aiXxx(...) 语句。"
        )
        script = self.llm.chat_text(system, user)

        # 程序校验（护栏）：脚本必须包含断言节点，否则补充；剥离 import / async 包裹
        script = self._normalize_script(script)
        if "aiAssert" not in script:
            script += "\nawait agent.aiWaitFor('操作完成');\nawait agent.aiAssert('操作成功');"
        return script

    @staticmethod
    def _normalize_script(script: str) -> str:
        # 去掉 import 行、async 包裹、export 等
        lines = []
        in_block = False
        for raw in script.splitlines():
            line = raw.strip()
            if line.startswith("import ") or line.startswith("export ") or line.startswith("const "):
                continue
            if line.startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                lines.append(raw)
            elif line:
                lines.append(raw)
        text = "\n".join(lines)
        # 去掉 async () => { ... } 包裹
        if text.strip().startswith("(async () =>") or text.strip().startswith("async function"):
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                text = text[start + 1 : end]
        return text.strip()

    # ---------------- 执行 + 自愈 ----------------
    def _execute_with_retry(self, task_id: str, script: str, base_url: str) -> dict:
        if not self.settings.ui_service_enabled:
            # 未启用 UI 服务：直接尝试一次，不可达则明确降级
            try:
                return self._call_service(task_id, script, base_url)
            except Exception as e:  # noqa: BLE001
                return {
                    "status": "error",
                    "mode": "unreachable",
                    "error": f"UI 执行服务不可达（{e}）。请启动: cd ui_service && npm install && npm start，并设置 AI_TEST_UI_SERVICE_ENABLED=true",
                    "results": [{"name": "ui_flow", "status": "error", "error": "执行服务未启用"}],
                }

        last: dict = {}
        for attempt in range(MAX_RETRY + 1):
            try:
                result = self._call_service(task_id, script, base_url)
            except Exception as e:  # noqa: BLE001
                last = {"status": "error", "error": str(e), "results": [], "screenshots": []}
                break
            last = result
            if result.get("status") == "success":
                return result
            # 失败：分析是否可自愈
            err = json.dumps(result.get("error") or result.get("results"), ensure_ascii=False)
            if self._recoverable(err):
                logger.warning("UI 用例第 %s 次执行失败，尝试自愈: %s", attempt + 1, err[:120])
                time.sleep(1)
                continue
            return result  # 不可自愈的错误（如业务断言失败），直接返回
        # 重试超限
        last["self_healed"] = False
        last["error"] = f"{last.get('error', '')}\n[自愈] 重试超过 {MAX_RETRY} 次，标记为真实失败"
        return last

    def _call_service(self, task_id: str, script: str, base_url: str) -> dict:
        resp = requests.post(
            f"{self.settings.ui_service_url}/api/ui/run",
            json={"task_id": task_id, "test_code": script, "base_url": base_url, "timeout": 60000},
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _recoverable(error_text: str) -> bool:
        """可自愈特征：定位失败 / 超时 / 网络抖动。"""
        markers = ["selector", "not found", "找不到", "timeout", "超时", "waiting for", "net::", "ECONN"]
        return any(m in error_text.lower() for m in markers)

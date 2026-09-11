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
import re
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

    def run(self, task: TestTask, plan: TestPlan, base_url: str | None = None, rag_context: str = "") -> dict:
        set_agent_context("ui_tester")
        base_url = base_url or task.meta.get("ui_base_url") or "https://demo.example.com"

        # 1) 生成脚本（B12：知识库三类知识注入；A17：步骤注释结构化映射；A18：防幻觉执行层校验）
        script, step_map = self._generate_script(plan, task.requirement, base_url, rag_context=rag_context)
        # A18：关键步骤保全校验（如"清空输入框"等细节不得省略）
        guard_warnings = self._validate_step_completeness(plan, task.requirement, step_map)
        if self.artifacts:
            self.artifacts.write("ui_tester", "ui_test_cases", script, "ts")
            self.artifacts.write("ui_tester", "ui_step_map", step_map, "json")
            self.artifacts.write("ui_tester", "guard_warnings", guard_warnings, "json")

        # 2) 执行（带自愈重试 + C8 错误对策映射）
        result, script, step_map = self._execute_with_retry(task.task_id, script, base_url)
        log_event(logger, "ui_done", {"status": result["status"], "mode": result.get("mode"),
                                      "steps": len(step_map),
                                      "warnings": len(guard_warnings)})

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
            "step_map": step_map,                # A17：步骤映射（双向追溯）
            "steps": len(step_map),
            "guard_warnings": guard_warnings,    # A18：防幻觉执行层校验结果
            "countermeasures": result.get("countermeasures", []),  # C8：错误对策记录
        }

    # ---------------- 用例生成 ----------------
    def _generate_script(self, plan: TestPlan, requirement: str, base_url: str, rag_context: str = "") -> tuple[str, list[dict]]:
        system = (Path(__file__).parent.parent / "prompts" / "ui_tester.md").read_text(encoding="utf-8")
        user = (
            f"ui_script 任务：为以下 UI 测试需求生成脚本\n"
            f"测试需求：{requirement}\n"
            f"目标地址：{base_url}\n"
            f"测试计划：{json.dumps(plan.to_dict(), ensure_ascii=False)}\n"
            # A17 结构化映射：要求每个可执行语句行前标注 // Step {序号}: {动作} 注释（回链用例步骤）
            "输出要求：只输出可执行脚本主体（不含 import，不含 async 包裹，agent 已注入），"
            "每行一个 await agent.aiXxx(...) 语句；每个可执行语句行前必须加一行注释 "
            "// Step {用例序号}.{步骤号}: {动作描述}（如 // Step 1.1: 输入用户名），用于与原用例步骤双向回链。"
        )
        # B12：知识库三类知识（操作手册/基线用例/上线检查清单）注入，附来源引用
        if rag_context:
            user += f"\n【知识库参考上下文（严格参考，来源可追溯）】\n{rag_context}"
        script = self.llm.chat_text(system, user)

        # 程序校验（护栏）：脚本必须包含断言节点，否则补充；剥离 import / async 包裹
        script = self._normalize_script(script)
        if "aiAssert" not in script:
            script += "\nawait agent.aiWaitFor('操作完成');\nawait agent.aiAssert('操作成功');"

        # A17 防幻觉执行层：强制每个可执行语句带 // Step 注释（缺则程序补号），并生成步骤映射
        script, step_map = self._ensure_step_comments(script)
        return script, step_map

    @staticmethod
    def _ensure_step_comments(script: str) -> tuple[str, list[dict]]:
        """A17 结构化映射保真 + A18 防幻觉执行层：多语句拆行、逐行编号、缺注释自动补号。

        返回 (脚本, step_map)，step_map = [{step, line, code}]，供执行报告回链步骤。
        """
        script = UITester._split_multi_statements(script)
        out_lines: list[str] = []
        step_map: list[dict] = []
        case_no = 1
        step_no = 1
        pending_step: str | None = None   # 已见到的 // Step 注释，等待绑定到下一个可执行语句
        for idx, raw in enumerate(script.splitlines()):
            line = raw.strip()
            if not line:
                out_lines.append(raw)
                continue
            m = re.match(r"^//\s*Step\s+([\d.]+)\s*[:：]?\s*(.*)$", line)
            if m:
                pending_step = m.group(1)
                out_lines.append(raw)
                continue
            # 可执行语句：绑定步骤号（有 LLM 注释用原号，无则程序补号）
            if line.startswith("await agent.") or line.startswith("await "):
                if pending_step:
                    step_key, desc = pending_step, ""
                    pending_step = None
                else:
                    step_key = f"{case_no}.{step_no}"
                    desc = line[:60]
                    out_lines.append(f"// Step {step_key}: {desc}")
                    step_no += 1
                step_map.append({"step": step_key, "line": len(out_lines), "code": line})
                out_lines.append(raw)
                continue
            # 其它行（注释/空结构/import 残留）：原样保留
            out_lines.append(raw)
        return "\n".join(out_lines), step_map

    @staticmethod
    def _split_multi_statements(script: str) -> str:
        """A18 防幻觉：禁止把多个步骤合成一行（多语句行按 await agent.X(...); 拆分为独立行），
        保证每个步骤单独编号、可追溯，杜绝"简化合并步骤"。"""
        out: list[str] = []
        for raw in script.splitlines():
            line = raw.strip()
            if line.startswith("//") or not line.startswith("await"):
                out.append(raw)
                continue
            # 匹配同一行内多个 await agent.aiXxx(...) 语句（不跨括号贪婪）
            parts = re.findall(r"await\s+agent\.[a-zA-Z]+\([^;]*?\)\s*;", line)
            if len(parts) > 1:
                for p in parts:
                    out.append(p)
            else:
                out.append(raw)
        return "\n".join(out)

    @staticmethod
    def _validate_step_completeness(plan, requirement: str, step_map: list[dict]) -> list[str]:
        """A18 关键步骤保全校验：需求/计划提及"清空/重置"等细节时，脚本必须包含对应动作，
        否则产出告警（防 LLM 省略步骤）。"""
        warnings: list[str] = []
        script_text = "\n".join(s["code"] for s in step_map)
        plan_text = str(getattr(plan, "to_dict", lambda: {})())
        haystack = f"{requirement} {plan_text}"
        must_have = [w for w in ("清空", "重置", "清屏") if w in haystack]
        for w in must_have:
            if w not in script_text:
                warnings.append(f"【A18 防幻觉】需求/计划提到「{w}」，但脚本中未发现对应步骤，可能被省略，请人工确认")
        # 结构保真：可执行步骤数为 0 属异常
        if not step_map:
            warnings.append("【A18 防幻觉】脚本无可执行步骤（生成异常）")
        return warnings

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

    # ---------------- 执行 + 自愈（C8 错误对策映射） ----------------
    def _execute_with_retry(self, task_id: str, script: str, base_url: str) -> tuple[dict, str, list[dict]]:
        """执行脚本；失败时按 C8 错误速查表分类并注入对策重试（≤MAX_RETRY）。

        返回 (result, final_script, step_map)：自愈注入的对策行会重新编号，保证步骤映射一致。
        """
        if not self.settings.ui_service_enabled:
            # 未启用 UI 服务：直接尝试一次，不可达则明确降级
            try:
                result = self._call_service(task_id, script, base_url)
                return result, script, UITester._ensure_step_comments(script)[1]
            except Exception as e:  # noqa: BLE001
                return {
                    "status": "error",
                    "mode": "unreachable",
                    "error": f"UI 执行服务不可达（{e}）。请启动: cd ui_service && npm install && npm start，并设置 AI_TEST_UI_SERVICE_ENABLED=true",
                    "results": [{"name": "ui_flow", "status": "error", "error": "执行服务未启用"}],
                    "countermeasures": [],
                }, script, UITester._ensure_step_comments(script)[1]

        last: dict = {}
        applied: list[dict] = []
        for attempt in range(MAX_RETRY + 1):
            try:
                result = self._call_service(task_id, script, base_url)
            except Exception as e:  # noqa: BLE001
                last = {"status": "error", "error": str(e), "results": [], "screenshots": [],
                        "countermeasures": applied}
                break
            last = result
            if result.get("status") == "success":
                result["countermeasures"] = applied
                return result, script, UITester._ensure_step_comments(script)[1]
            # 失败：C8 错误分类 → 可自愈则注入对策重试
            err = json.dumps(result.get("error") or result.get("results"), ensure_ascii=False)
            recoverable, error_type, countermeasures = self._classify_error(err)
            if recoverable:
                record = {"attempt": attempt + 1, "error_type": error_type, "error": err[:120],
                          "countermeasures": countermeasures}
                applied.append(record)
                logger.warning("UI 用例第 %s 次执行失败（%s），注入对策重试: %s", attempt + 1, error_type, countermeasures)
                script = self._apply_countermeasure(script, error_type)
                script, _ = self._ensure_step_comments(script)   # 重新编号，保持步骤映射一致
                time.sleep(1)
                continue
            result["countermeasures"] = applied
            return result, script, UITester._ensure_step_comments(script)[1]
        # 重试超限
        last["self_healed"] = False
        last["countermeasures"] = applied
        last["error"] = f"{last.get('error', '')}\n[自愈] 重试超过 {MAX_RETRY} 次，标记为真实失败"
        return last, script, UITester._ensure_step_comments(script)[1]

    @staticmethod
    def _classify_error(error_text: str) -> tuple[bool, str, list[str]]:
        """C8 错误速查表：错误特征 → 错误类型 → 对策清单。

        返回 (是否可自愈, 错误类型, 对策列表)。
        """
        text = error_text.lower()
        if any(m in text for m in ("selector", "not found", "找不到", "cannot locate", "无法定位", "element")):
            return True, "selector_not_found", [
                "在定位描述中补充容器上下文 + 视觉位置 + 文案 + 元素类型",
                "确认页面已加载目标容器后再定位（可先 aiWaitFor 目标文案）",
            ]
        if any(m in text for m in ("timeout", "超时", "waiting for", "waited", "timed out")):
            return True, "timeout", [
                "为关键节点增加 aiWaitFor 等待特定状态（已自动注入稳定化等待）",
                "必要时延长超时时间（默认 5s）",
            ]
        if any(m in text for m in ("net::", "econn", "connection", "网络", "socket", "reset")):
            return True, "network", [
                "网络抖动：1 秒后自动重试",
                "检查执行服务与目标站点可达性",
            ]
        return False, "business_assert", []   # 业务断言失败 = 功能缺陷，不自动修复

    @staticmethod
    def _apply_countermeasure(script: str, error_type: str) -> str:
        """C8：按错误类型注入对策到脚本（timeout → 稳定化 aiWaitFor；selector → 通用等待）。"""
        if error_type in ("timeout", "selector_not_found"):
            stab = "await agent.aiWaitFor('页面关键元素就绪', timeout_ms=10000);"
            if stab not in script:
                return f"// Step 0.0: [C8自动对策-{error_type}] 注入稳定化等待\n{stab}\n{script}"
        return script

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
        """兼容入口：仅判断是否可自愈（C8 分类的布尔投影）。"""
        return UITester._classify_error(error_text)[0]

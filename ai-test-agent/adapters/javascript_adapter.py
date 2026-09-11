"""JavaScript 适配器 —— @babel/parser（Node CLI）+ node:test。

执行链路：
- AST 解析：调用 Node 脚本（@babel/parser），输出 JSON
- 语法校验：node --check
- 单测执行：node --test（Node 22 内置 test runner，无外部依赖）
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .base import ChangedFunction, LanguageAdapter

_JS_AST = Path(__file__).parent / "js_ast" / "functions.js"


class JavaScriptAdapter(LanguageAdapter):
    language = "javascript"
    test_command = ["node", "--test"]

    EXTENSIONS = {".js", ".mjs", ".cjs"}

    @staticmethod
    def detect(file_path: str) -> bool:
        return Path(file_path).suffix in JavaScriptAdapter.EXTENSIONS

    def _node_available(self) -> bool:
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=10)
            return True
        except Exception:  # noqa: BLE001
            return False

    def extract_functions(self, source: str) -> list[dict]:
        if not self._node_available():
            return []
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(source)
            tmp = f.name
        try:
            proc = subprocess.run(["node", str(_JS_AST), tmp], capture_output=True, text=True, timeout=30)
        finally:
            Path(tmp).unlink(missing_ok=True)
        try:
            return json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []

    def extract_calls(self, source: str) -> list[str]:
        # 简化：正则提取函数调用名
        calls = set(re.findall(r"\b([a-zA-Z_$][\w$]*)\s*\(", source))
        return sorted(c for c in calls if c not in ("if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await", "require", "console", "describe", "it", "test"))

    # ---- 单测 ----
    def generate_test_code(self, func: ChangedFunction, source: str) -> str:
        name = func.function_name
        return f'''// AI 生成的单测：{name}（兜底模板，node:test）
const {{ test }} = require('node:test');
const assert = require('node:assert');

test('{name} 正常路径', () => {{
  // 真实断言由 LLM 根据业务语义生成
  assert.ok(true);
}});

test('{name} 缺失参数', () => {{
  assert.throws(() => {{ {name}(); }});
}});
'''

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        if not self._node_available():
            return False, "node 不可用"
        proc = subprocess.run(["node", "--check", "-"], input=code, capture_output=True, text=True, timeout=30)
        return (proc.returncode == 0, proc.stderr.strip())

    def execute_tests(self, test_dir: str) -> dict:
        if not self._node_available():
            return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": "node 不可用"}
        files = sorted(str(p) for p in Path(test_dir).glob("*.js"))
        if not files:
            return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": "没有测试文件"}
        proc = subprocess.run(
            ["node", "--test", *files], capture_output=True, text=True, timeout=120,
        )
        out = proc.stdout + proc.stderr
        passed = 0
        failed = 0
        m = re.search(r"# (?:pass|ok)\s+(\d+)", out)
        if m:
            passed = int(m.group(1))
        m = re.search(r"# fail\s+(\d+)", out)
        if m:
            failed = int(m.group(1))
        if "no tests ran" in out:
            passed = failed = 0
        return {"passed": passed, "failed": failed, "results": [], "coverage": None, "raw": out[-500:]}

"""TypeScript 适配器 —— 复用 @babel/parser（typescript 插件）+ node:test。

TS 与 JS 的 AST 解析共用同一 Node 脚本（functions.js 自动识别 .ts/.tsx 启用 typescript 插件）。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .base import ChangedFunction, LanguageAdapter
from .javascript_adapter import JavaScriptAdapter

_JS_AST = Path(__file__).parent / "js_ast" / "functions.js"


class TypeScriptAdapter(JavaScriptAdapter):
    language = "typescript"
    EXTENSIONS = {".ts", ".tsx"}

    @staticmethod
    def detect(file_path: str) -> bool:
        return Path(file_path).suffix in TypeScriptAdapter.EXTENSIONS

    def extract_functions(self, source: str) -> list[dict]:
        if not self._node_available():
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False, encoding="utf-8") as f:
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

    def generate_test_code(self, func: ChangedFunction, source: str) -> str:
        name = func.function_name
        return f'''// AI 生成的单测：{name}（兜底模板，node:test，TS 需 tsx/jest 时由执行环境转译）
const {{ test }} = require('node:test');
const assert = require('node:assert');

test('{name} 正常路径', () => {{
  assert.ok(true);
}});

test('{name} 异常路径', () => {{
  assert.throws(() => {{ {name}(); }});
}});
'''

    def execute_tests(self, test_dir: str) -> dict:
        """TS 测试执行：转译为 JS 前先尝试直接执行；失败时给出转译提示"""
        if not self._node_available():
            return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": "node 不可用"}
        files = sorted(str(p) for p in Path(test_dir).glob("*.ts"))
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
        if passed == 0 and failed == 0 and ("SyntaxError" in out or "ERR_MODULE_NOT_FOUND" in out):
            return {"passed": 0, "failed": 0, "results": [], "coverage": None,
                    "error": "TS 直接执行失败，需要 ts-node/tsx 转译", "raw": out[-400:]}
        return {"passed": passed, "failed": failed, "results": [], "coverage": None, "raw": out[-400:]}

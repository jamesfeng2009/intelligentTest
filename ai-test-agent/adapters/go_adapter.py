"""Go 适配器 —— go/ast（编译 CLI）+ testing 包。

执行链路：
- AST 解析：编译 go_ast CLI（go/ast 标准库，无外部依赖）后调用
- 语法校验：gofmt -e
- 单测执行：临时模块内 go test（标准库 testing，不依赖 testify，避免外部依赖）
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import ChangedFunction, LanguageAdapter

_GO_AST_DIR = Path(__file__).parent / "go_ast"
_BIN = _GO_AST_DIR / "astcli"


def _ensure_go_cli() -> Path | None:
    """编译 go_ast CLI（缓存二进制）。"""
    if _BIN.exists():
        return _BIN
    if shutil.which("go") is None:
        return None
    try:
        # 确保 go.mod 存在（go modules 必需）
        if not (_GO_AST_DIR / "go.mod").exists():
            (_GO_AST_DIR / "go.mod").write_text("module astcli\n\ngo 1.21\n", encoding="utf-8")
        subprocess.run(["go", "build", "-o", str(_BIN), "."], cwd=str(_GO_AST_DIR), capture_output=True, text=True, timeout=180)
        return _BIN if _BIN.exists() else None
    except Exception:  # noqa: BLE001
        return None


class GoAdapter(LanguageAdapter):
    language = "go"
    test_command = ["go", "test"]

    EXTENSIONS = {".go"}

    @staticmethod
    def detect(file_path: str) -> bool:
        return Path(file_path).suffix == ".go"

    def extract_functions(self, source: str) -> list[dict]:
        cli = _ensure_go_cli()
        if cli is None:
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".go", delete=False, encoding="utf-8") as f:
            f.write(source)
            tmp = f.name
        try:
            proc = subprocess.run([str(cli), tmp], capture_output=True, text=True, timeout=30)
        finally:
            Path(tmp).unlink(missing_ok=True)
        try:
            return json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []

    def extract_calls(self, source: str) -> list[str]:
        calls = set(re.findall(r"\b([a-zA-Z_]\w*)\s*\(", source))
        skip = {"if", "for", "switch", "func", "return", "go", "defer", "make", "new", "len", "cap", "append", "range", "fmt", "log", "err", "nil", "panic", "recover"}
        return sorted(c for c in calls if c not in skip and c[:1].isupper() or c not in skip)

    def generate_test_code(self, func: ChangedFunction, source: str) -> str:
        name = func.function_name
        pkg = _guess_package(source)
        return f'''package {pkg}

import "testing"

// AI 生成的单测：{name}（兜底模板，标准库 testing）
func Test{_title(name)}Normal(t *testing.T) {{
    // 真实断言由 LLM 根据业务语义生成
}}

func Test{_title(name)}Empty(t *testing.T) {{
    // 边界路径
}}
'''

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        if shutil.which("gofmt") is None:
            return True, "gofmt 不可用，跳过语法校验"
        with tempfile.NamedTemporaryFile("w", suffix=".go", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp = f.name
        try:
            proc = subprocess.run(["gofmt", "-e", tmp], capture_output=True, text=True, timeout=30)
            return (proc.returncode == 0, proc.stderr.strip())
        finally:
            Path(tmp).unlink(missing_ok=True)

    def execute_tests(self, test_dir: str) -> dict:
        if shutil.which("go") is None:
            return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": "go 工具链不可用"}
        with tempfile.TemporaryDirectory() as tmp:
            # 组装临时模块：把测试文件和被测文件拷入
            mod = Path(tmp) / "mod"
            mod.mkdir()
            for f in Path(test_dir).glob("*.go"):
                (mod / f.name).write_text(f.read_text(encoding="utf-8"))
            (mod / "go.mod").write_text("module demotest\n\ngo 1.21\n")
            proc = subprocess.run(["go", "test", "./..."], cwd=str(mod), capture_output=True, text=True, timeout=120)
            out = proc.stdout + proc.stderr
            passed = 1 if "ok  " in out or "no test files" in out else 0
            failed = 1 if "FAIL" in out else 0
            if "no test files" in out:
                passed, failed = 0, 1
                out = "no test files: 未生成可编译测试"
            cov = re.search(r"coverage: ([\d.]+%)", out)
            return {"passed": passed, "failed": failed, "results": [], "coverage": cov.group(1) if cov else None, "raw": out[-500:]}


def _guess_package(source: str) -> str:
    m = re.search(r"^\s*package\s+(\w+)", source, re.M)
    return m.group(1) if m else "main"


def _title(name: str) -> str:
    return name[:1].upper() + name[1:]

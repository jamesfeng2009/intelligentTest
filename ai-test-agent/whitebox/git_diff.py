"""Git Diff 四层变更分析（对齐项目计划 §3.4）。

第一层 文件级：git diff --name-status
第二层 行级：解析 hunk 头（@@ -a,b +c,d @@）
第三层 函数级：AST 解析，把变更行映射到函数（各语言适配器）
第四层 影响面：见 impact_analysis.py
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from adapters.factory import get_adapter
from adapters.base import ChangedFunction
from core.logging_setup import get_logger

logger = get_logger("git_diff")


class GitDiffError(Exception):
    pass


@dataclass
class ChangedFile:
    path: str
    change_type: str                # modified / added / deleted / renamed
    added_lines: int = 0
    deleted_lines: int = 0
    hunks: list[tuple[int, int]] = field(default_factory=list)
    functions: list[ChangedFunction] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "added_lines": self.added_lines,
            "deleted_lines": self.deleted_lines,
            "hunks": [list(h) for h in self.hunks],
            "functions": [f.to_dict() for f in self.functions],
        }


def _git(repo_path: str, args: list[str], timeout: int = 60) -> str:
    proc = subprocess.run(["git", *args], cwd=repo_path, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise GitDiffError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def get_changed_files(repo_path: str, base_commit: str, target_commit: str) -> list[ChangedFile]:
    """第一层：文件级变更。"""
    out = _git(repo_path, ["diff", "--name-status", base_commit, target_commit])
    files: list[ChangedFile] = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        change_type = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed"}.get(status[0], "modified")
        files.append(ChangedFile(path=path, change_type=change_type))
    return files


def get_diff_hunks(repo_path: str, base_commit: str, target_commit: str, file_path: str) -> list[tuple[int, int]]:
    """第二层：行级变更（hunk 解析，新文件行号范围）。"""
    out = _git(repo_path, ["diff", base_commit, target_commit, "--", file_path])
    hunks: list[tuple[int, int]] = []
    for line in out.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                hunks.append((start, start + count - 1))
    return hunks


def get_changed_functions(repo_path: str, base_commit: str, target_commit: str) -> list[ChangedFunction]:
    """第三层：函数级变更。变更行 → AST 函数映射。"""
    files = get_changed_files(repo_path, base_commit, target_commit)
    changed_funcs: list[ChangedFunction] = []
    for cf in files:
        if cf.change_type == "deleted":
            continue
        full_path = Path(repo_path) / cf.path
        if not full_path.exists():
            continue
        adapter = get_adapter(cf.path)
        if adapter is None:
            continue
        hunks = get_diff_hunks(repo_path, base_commit, target_commit, cf.path)
        cf.hunks = hunks
        try:
            source = full_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        funcs = adapter.extract_functions(source)
        for func in funcs:
            start, end = func["start_line"], func["end_line"]
            for h_start, h_end in hunks:
                if h_start <= end and h_end >= start:
                    changed_lines = list(range(max(h_start, start), min(h_end, end) + 1))
                    sig = _extract_signature(source, start)
                    changed_funcs.append(ChangedFunction(
                        file_path=cf.path,
                        function_name=func["name"],
                        start_line=start,
                        end_line=end,
                        changed_lines=changed_lines,
                        source_code=_extract_source_block(source, start, end),
                        signature=sig,
                        full_source=source,
                    ))
                    break
        cf.functions = [f for f in changed_funcs if f.file_path == cf.path]
    return changed_funcs


def _extract_signature(source: str, start_line: int) -> str:
    lines = source.splitlines()
    if start_line - 1 >= len(lines):
        return ""
    # 函数头通常跨 1-3 行
    sig = lines[start_line - 1]
    for i in range(1, 3):
        if sig.count("(") <= sig.count(")"):
            break
        if start_line - 1 + i < len(lines):
            sig += " " + lines[start_line - 1 + i].strip()
    return sig.strip()


def _extract_source_block(source: str, start: int, end: int) -> str:
    import textwrap

    lines = source.splitlines()
    block = "\n".join(lines[max(0, start - 1): min(len(lines), end)])
    # 类内方法提取出来是带缩进的，dedent 后才能独立 exec
    return textwrap.dedent(block)

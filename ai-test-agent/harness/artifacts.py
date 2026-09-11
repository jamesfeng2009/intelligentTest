"""产物契约 —— 文件系统隔离。

每个 run 拥有独立产物目录（artifacts/runs/<task_id>/），子 Agent 只能读写自己的子目录。
产物以 JSON/MD 结构化落盘，形成可留痕、可复盘、可追踪的执行轨迹（Trace）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_settings

# 产物类型 → 扩展名
_EXT = {
    "json": ".json",
    "md": ".md",
    "code": ".py",
    "ts": ".ts",
    "log": ".log",
    "png": ".png",
    "html": ".html",
}

# 目录契约：哪个 Agent 写哪个子目录
AGENT_DIRS = {
    "orchestrator": "orchestrator",
    "api_tester": "api_tester",
    "ui_tester": "ui_tester",
    "whitebox_tester": "whitebox_tester",
    "functional_tester": "functional_tester",
    "functional_reviewer": "functional_reviewer",
    "security_tester": "security_tester",
    "verifier": "verifier",
}

_SAFE = re.compile(r"[^a-zA-Z0-9_\-.]")


class ArtifactError(Exception):
    pass


class ArtifactManager:
    """按 task_id 管理一次运行的产物。"""

    def __init__(self, task_id: str, base_dir: Path | None = None) -> None:
        self.task_id = _SAFE.sub("_", task_id)
        settings = get_settings()
        base = base_dir or settings.artifacts_dir
        self.run_dir = base / "runs" / self.task_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----
    def agent_dir(self, agent: str) -> Path:
        if agent not in AGENT_DIRS:
            raise ArtifactError(f"未知 Agent 目录契约: {agent}，允许: {list(AGENT_DIRS)}")
        d = self.run_dir / AGENT_DIRS[agent]
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- 写 ----
    def write(self, agent: str, name: str, data: Any, kind: str = "json") -> Path:
        """按契约写入产物。data 为 dict/list 时写 JSON；kind=md/code 时写文本。"""
        d = self.agent_dir(agent)
        ext = _EXT.get(kind, ".json")
        path = d / f"{name}{ext}"
        if kind in ("json",):
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.write_text(str(data), encoding="utf-8")
        return path

    def append_log(self, agent: str, line: str) -> Path:
        d = self.agent_dir(agent)
        path = d / "execution.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
        return path

    def save_screenshot(self, agent: str, name: str, binary: bytes) -> Path:
        d = self.agent_dir(agent)
        path = d / f"{_SAFE.sub('_', name)}.png"
        path.write_bytes(binary)
        return path

    # ---- 读 ----
    def read(self, agent: str, name: str) -> Any:
        """读取 JSON 产物；不存在返回 None。"""
        d = self.agent_dir(agent)
        path = d / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def read_text(self, agent: str, name: str, kind: str = "md") -> str | None:
        ext = _EXT.get(kind, ".json")
        path = self.agent_dir(agent) / f"{name}{ext}"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_files(self) -> list[dict]:
        out = []
        for root, _, files in os.walk(self.run_dir):
            for f in sorted(files):
                p = Path(root) / f
                out.append({
                    "path": str(p.relative_to(self.run_dir)),
                    "size": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                })
        return out

    # ---- 汇总 ----
    def collect_artifacts(self) -> dict[str, list[str]]:
        """按 agent 汇总全部产物路径（供报告引用）。"""
        result: dict[str, list[str]] = {}
        for agent in AGENT_DIRS:
            d = self.run_dir / AGENT_DIRS[agent]
            if d.exists():
                result[agent] = [str(p.relative_to(self.run_dir)) for p in sorted(d.iterdir()) if p.is_file()]
        return result

    def cleanup(self) -> None:
        shutil.rmtree(self.run_dir, ignore_errors=True)

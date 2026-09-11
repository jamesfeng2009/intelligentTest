"""前后端工程识别（A8）：按仓库特征判定工程类型。

白盒分析前先判定被测工程形态，避免用错分析视角：
- backend：后端服务（Spring/Go/FastAPI/Django/Flask/pom/gradle/go.mod/requirements）
- frontend：前端工程（Vue/React/Angular/vite/next/package.json 依赖）
- fullstack：前后端同仓
- unknown：特征不足

识别依据为文件与目录特征 + 关键配置文件内容（package.json 依赖），证据可追溯。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_FRONTEND_DEPS = ("vue", "react", "angular", "@vue", "@angular", "next", "nuxt", "vite", "svelte", "umi", "antd")
_FRONTEND_FILES = ("vite.config.js", "vite.config.ts", "vue.config.js", "next.config.js",
                   "nuxt.config.ts", "src/main.tsx", "src/main.jsx", "src/App.vue", "src/router/index.ts")
_BACKEND_FILES = ("pom.xml", "build.gradle", "go.mod", "requirements.txt", "manage.py",
                  "application.yml", "application.yaml", "go.sum", "pyproject.toml", "setup.py")
_BACKEND_SRC_MARKERS = (
    ("src/main/java", "Java/Spring"),
    ("src/main/kotlin", "Kotlin/Spring"),
    ("app/main.py", "Python"),
    ("main.py", "Python"),
)
_BACKEND_CONTENT_MARKERS = (
    (r"\bFastAPI\b", "FastAPI"),
    (r"\bFlask\b", "Flask"),
    (r"\bDjango\b", "Django"),
    (r"@SpringBootApplication", "Spring Boot"),
    (r"\bgin\.Default\(\)", "Go Gin"),
    (r"\bspring-boot-starter", "Spring Boot"),
)


def detect_project_type(repo_path: str | Path) -> dict:
    """检测仓库工程类型。返回 {"type": backend|frontend|fullstack|unknown, "evidence": [str], "backend": bool, "frontend": bool}"""
    root = Path(repo_path)
    frontend_hits: list[str] = []
    backend_hits: list[str] = []

    def _walk_names() -> list[str]:
        out: list[str] = []
        try:
            for p in root.rglob("*"):
                if p.is_file() and not any(part in p.parts for part in (".git", "node_modules", "dist", "build", "target", ".venv", "venv")):
                    out.append(str(p.relative_to(root)))
        except OSError:
            pass
        return out[:8000]

    files = _walk_names()
    lower_files = {f.lower() for f in files}

    # 1) 关键配置文件 / 目录特征
    for f in _BACKEND_FILES:
        if f in lower_files:
            backend_hits.append(f)
    for f, label in _BACKEND_SRC_MARKERS:
        if any(x.startswith(f.lower()) for x in lower_files):
            backend_hits.append(label)
    for f in _FRONTEND_FILES:
        if f in lower_files:
            frontend_hits.append(f)

    # 2) 前端框架源码特征
    if any(f.endswith(".vue") for f in files):
        frontend_hits.append("Vue SFC (*.vue)")
    if any(f.endswith(".jsx") or f.endswith(".tsx") for f in files) and "node_modules" not in str(root):
        frontend_hits.append("React/JSX 组件")

    # 3) 包管理依赖特征（package.json 内容）
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            hit_deps = [d for d in deps if any(k in d.lower() for k in _FRONTEND_DEPS)]
            if hit_deps:
                frontend_hits.append(f"package.json 依赖: {', '.join(sorted(hit_deps)[:5])}")
            if any("jest" in d or "vitest" in d or "playwright" in d for d in deps):
                frontend_hits.append("前端测试框架依赖")
        except (OSError, json.JSONDecodeError):
            pass

    # 4) 后端框架内容特征
    for f in ("main.py", "app/main.py", "pom.xml", "build.gradle"):
        fp = root / f
        if fp.exists():
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")[:50000]
                for pat, label in _BACKEND_CONTENT_MARKERS:
                    if re.search(pat, text, re.I) and label not in backend_hits:
                        backend_hits.append(label)
            except OSError:
                pass

    is_frontend = bool(frontend_hits)
    is_backend = bool(backend_hits)
    if is_frontend and is_backend:
        ptype = "fullstack"
    elif is_frontend:
        ptype = "frontend"
    elif is_backend:
        ptype = "backend"
    else:
        ptype = "unknown"
    return {
        "type": ptype,
        "backend": is_backend,
        "frontend": is_frontend,
        "evidence": (frontend_hits + backend_hits)[:12],
    }

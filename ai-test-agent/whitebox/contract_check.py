"""契约变化检测（A10）—— 解决"局部正确、整体崩溃"。

变更的方法可能被其他未改动的文件调用；只看变更文件本身不够。本模块基于
基线 commit 对比 + 静态扫描调用方，专项检测四类契约风险：

1. 公共方法签名变化（参数增删/改名）→ 调用方参数不匹配
2. 新增枚举 / 枚举成员 → switch-case、if-elif、映射表是否全覆盖
3. DTO / 实体字段变更 → 序列化/反序列化、接口文档 schema、数据迁移是否同步
4. 接口路径 / 方法 / 参数变化 → 接口文档、网关路由、客户端版本兼容

输出 findings：{source, severity, type, location, issue, suggestion, evidence, callers}。
静态分析结果标注"可能有遗漏，建议人工复核"。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from adapters.factory import get_adapter
from core.logging_setup import get_logger

logger = get_logger("contract_check")

_ENUM_HEADER = re.compile(
    r"^\+\s*(?:export\s+)?(?:enum|class)\s+\w+|^\+\s*class\s+\w+\([^)]*Enum|^\+\s*\w+\s*=\s*(?:Enum|IntEnum|StrEnum)\(",
    re.I,
)
_ENUM_MEMBER = re.compile(r"^\+\s*[A-Z][A-Z0-9_]{1,}\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|\d+)")
_FIELD_LINE = re.compile(r"^[+-]\s*(?:self\.\w+\s*=|['\"]?\w+['\"]?\s*:\s*|@?dataclass|class\s+\w+)")
_ROUTE_LINE = re.compile(
    r"^[+-]\s*(?:@(?:app|router|bp|api)\.(?:get|post|put|delete|patch|route)\b|"
    r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping\b|"
    r"(?:app|router|bp|api)\.(?:get|post|put|delete|patch)\()",
    re.I,
)
_SWITCH_LIKE = re.compile(r"\b(?:switch|case)\b|elif\s|==\s*[\w.]+\.|in\s+[\w.]+\.", re.I)


def _git(repo_path: str, args: list[str], timeout: int = 60) -> str:
    proc = subprocess.run(["git", *args], cwd=repo_path, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _git_show(repo_path: str, commit: str, path: str) -> str | None:
    """取某 commit 下文件内容；不存在返回 None。"""
    out = _git(repo_path, ["show", f"{commit}:{path}"])
    return out if out else None


def _parse_params(signature: str) -> list[str]:
    """从函数签名提取参数名列表（去 self/cls，去默认值与注解）。"""
    m = re.search(r"\((.*?)\)", signature, re.S)
    if not m:
        return []
    params: list[str] = []
    for part in m.group(1).split(","):
        p = part.strip()
        if not p:
            continue
        p = p.split("=")[0].split(":")[0].strip()
        if p in ("self", "cls") or p.startswith(("*", "**")):
            continue
        params.append(p)
    return params


def _old_signature(repo_path: str, base_commit: str, func) -> str | None:
    """从基线 commit 的文件中定位同名函数，提取旧签名；文件不存在或函数不存在返回 None。"""
    old_src = _git_show(repo_path, base_commit, func.file_path)
    if not old_src:
        return None
    adapter = get_adapter(func.file_path)
    if adapter is None:
        return None
    try:
        funcs = adapter.extract_functions(old_src)
    except Exception:  # noqa: BLE001
        return None
    for f in funcs:
        if f["name"] == func.function_name:
            lines = old_src.splitlines()
            start = f["start_line"]
            if start - 1 >= len(lines):
                return None
            sig = lines[start - 1]
            for i in range(1, 3):
                if sig.count("(") <= sig.count(")"):
                    break
                if start - 1 + i < len(lines):
                    sig += " " + lines[start - 1 + i].strip()
            return sig.strip()
    return None


def _diff_added_lines(repo_path: str, base_commit: str, target_commit: str, path: str) -> list[str]:
    """取变更文件的 added 行（^+，排除 +++ 头）。"""
    out = _git(repo_path, ["diff", base_commit, target_commit, "--", path])
    added: list[str] = []
    for line in out.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


def _grep(repo_path: str, pattern: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.ts", "--include=*.go",
             "-E", pattern, repo_path],
            capture_output=True, text=True, timeout=60,
        )
        out = [ln for ln in proc.stdout.strip().splitlines() if ":" in ln and not ln.startswith(repo_path)]
        return out[:20]
    except Exception:  # noqa: BLE001
        return []


def detect_contract_changes(repo_path: str, base_commit: str, target_commit: str,
                            changed_funcs: list[Any]) -> list[dict]:
    """检测四类契约变化，返回 findings（静态分析，可能有遗漏）。"""
    findings: list[dict] = []
    files = sorted({f.file_path for f in changed_funcs})

    # ---- 1) 公共方法签名变化 ----
    for f in changed_funcs:
        new_params = _parse_params(f.signature)
        old_sig = _old_signature(repo_path, base_commit, f)
        if old_sig is None:
            continue  # 新增函数（基线不存在）无破坏性契约风险
        old_params = _parse_params(old_sig)
        if old_params == new_params:
            continue
        if not new_params and old_params:
            # 目标侧无法解析参数（如 multiline 解析失败），保守跳过并提示
            findings.append({
                "source": "契约变化检测(A10)", "severity": "警告", "type": "公共方法签名变化",
                "location": f"{f.file_path}:{f.function_name}",
                "issue": f"函数签名变化：{old_sig[:80]} → {f.signature[:80]}（参数解析不完整，需人工核对）",
                "suggestion": "人工确认实际参数变更并同步调用方", "evidence": [old_sig[:120]], "callers": [],
            })
            continue
        callers = _find_callers(repo_path, f.function_name, f.file_path)
        removed = [p for p in old_params if p not in new_params]
        added = [p for p in new_params if p not in old_params]
        severity = "高" if removed else "中"
        findings.append({
            "source": "契约变化检测(A10)", "severity": severity, "type": "公共方法签名变更",
            "location": f"{f.file_path}:{f.function_name}",
            "issue": f"公共方法签名参数变化：删除 {removed or '无'}、新增 {added or '无'}；调用方可能未同步",
            "suggestion": f"grep 调用方并逐个核对参数（当前扫描到 {len(callers)} 处调用）；必要时提供兼容重载/默认值",
            "evidence": [f"旧签名: {old_sig[:120]}", f"新签名: {f.signature[:120]}"],
            "callers": callers,
        })

    # ---- 2/3/4) 基于 diff 的检测（枚举 / DTO 字段 / 接口路由） ----
    for path in files:
        added = _diff_added_lines(repo_path, base_commit, target_commit, path)
        if not added:
            continue
        location = path
        # 2) 新增枚举 / 枚举成员（防误报：仅当同批 diff 出现枚举定义，或文件本身已定义枚举时，才把
        #    大写常量赋值判定为枚举成员，避免把普通模块级常量当成枚举）
        enum_headers = [ln.strip() for ln in added
                        if _ENUM_HEADER.search("+" + ln) and ("enum" in ln.lower() or "Enum" in ln)]
        target_src = _git_show(repo_path, target_commit, path) or ""
        file_has_enum = bool(re.search(r"\benum\b|\w+Enum\b", target_src))
        enum_members: list[str] = []
        if enum_headers or file_has_enum:
            enum_members = [ln.strip() for ln in added if _ENUM_MEMBER.match("+" + ln)]
        if enum_headers or (file_has_enum and enum_members):
            type_names = re.findall(r"(?:enum|class)\s+(\w+)", " ".join(enum_headers)) or []
            member_names = [m.split()[0].lstrip("+") for m in enum_members]
            usages: list[str] = []
            for tn in type_names:
                usages.extend(_grep(repo_path, rf"\b{tn}\b"))
            findings.append({
                "source": "契约变化检测(A10)", "severity": "高", "type": "新增枚举/枚举成员",
                "location": location,
                "issue": f"新增枚举类型 {type_names or '?'} / 枚举成员 {member_names[:6] or '?'}：switch-case、if-elif、映射表可能未覆盖",
                "suggestion": "核对所有使用该枚举的 switch/case、映射表、前端展示与接口 schema 是否覆盖新增成员",
                "evidence": [f"新增枚举成员 {len(enum_members)} 个", f"枚举引用扫描 {len(usages)} 处"],
                "callers": usages[:10],
            })
        # 3) DTO / 实体字段变更（模型/实体/schema 类文件）
        is_model_file = any(k in path.lower() for k in ("model", "entity", "dto", "schema", "domain", "vo"))
        if is_model_file:
            field_changes = [ln.strip() for ln in added if _FIELD_LINE.search("+" + ln)
                             and not ln.strip().startswith(("class ", "@"))]
            if field_changes:
                findings.append({
                    "source": "契约变化检测(A10)", "severity": "中", "type": "DTO/实体字段变更",
                    "location": location,
                    "issue": f"DTO/实体字段变化 {len(field_changes)} 处（如 {field_changes[0][:60]}）：序列化/反序列化、数据库迁移、接口文档 schema 需同步",
                    "suggestion": "核对接口文档 OpenAPI schema、数据库表结构、调用方 JSON 字段是否同步",
                    "evidence": [f"新增/变更字段行 {len(field_changes)} 处"],
                    "callers": [],
                })
        # 4) 接口路径 / 方法 / 参数变化
        route_changes = [ln.strip() for ln in added if _ROUTE_LINE.search("+" + ln)]
        if route_changes:
            findings.append({
                "source": "契约变化检测(A10)", "severity": "高", "type": "接口路径/方法/参数变化",
                "location": location,
                "issue": f"接口路由定义变化 {len(route_changes)} 处（如 {route_changes[0][:60]}）：接口文档、网关路由、客户端调用方需同步",
                "suggestion": "同步 OpenAPI/接口文档，检查网关路由与客户端版本兼容，必要时提供版本兼容接口",
                "evidence": [f"路由定义变化 {len(route_changes)} 处"],
                "callers": [],
            })

    # 静态分析免责标注
    if findings:
        logger.warning("契约变化检测输出 %s 条，静态分析可能有遗漏，建议人工复核", len(findings))
    return findings


def _find_callers(repo_path: str, function_name: str, changed_file: str) -> list[str]:
    """grep 调用方（简单版，与 impact_analysis 一致）。"""
    callers: set[str] = set()
    for ext in ("*.py", "*.js", "*.ts", "*.tsx", "*.go"):
        try:
            proc = subprocess.run(
                ["grep", "-rn", "--include=" + ext, rf"{function_name}\s*\(", repo_path],
                capture_output=True, text=True, timeout=60,
            )
            for line in proc.stdout.strip().splitlines():
                if ":" in line:
                    fp = line.split(":")[0]
                    if fp != changed_file:
                        callers.add(fp)
        except Exception:  # noqa: BLE001
            continue
    return sorted(callers)

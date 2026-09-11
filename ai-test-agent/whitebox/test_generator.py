"""单测生成器 —— LLM 生成 + 语言适配器兜底 + 语法校验 + 执行。"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from adapters.base import ChangedFunction, LanguageAdapter
from core.llm import BaseLLM
from core.logging_setup import get_logger

logger = get_logger("test_generator")


def generate_and_execute(
    llm: BaseLLM,
    funcs: list[ChangedFunction],
    adapter: LanguageAdapter,
    max_batch: int = 10,
) -> dict:
    """为一批变更函数生成单测并执行。

    返回：{generated, passed, failed, results, coverage, code_map}
    """
    code_map: dict[str, str] = {}
    all_codes: list[str] = []

    # 1) 生成：LLM 优先，适配器模板兜底
    for i in range(0, len(funcs), max_batch):
        batch = funcs[i : i + max_batch]
        generated = _llm_generate(llm, batch, adapter)
        for f, code in generated:
            code_map[f.function_name] = code
            all_codes.append(code)
        # 兜底：LLM 未覆盖的函数用适配器模板
        covered = {f.function_name for f, _ in generated}
        for f in batch:
            if f.function_name not in covered:
                code = adapter.generate_test_code(f, f.source_code)
                code_map[f.function_name] = code
                all_codes.append(code)

    # 2) 语法校验（护栏：语法错误丢弃）
    valid_codes: list[str] = []
    for code in all_codes:
        ok, err = adapter.validate_syntax(code)
        if ok:
            valid_codes.append(code)
        else:
            logger.warning("单测语法校验失败，丢弃: %s", err[:120])

    # 3) 执行
    execution = _execute(adapter, valid_codes, funcs=list(funcs))
    execution["generated"] = len(all_codes)
    execution["code_map"] = code_map
    return execution


def _llm_generate(llm: BaseLLM, batch: list[ChangedFunction], adapter: LanguageAdapter) -> list[tuple[ChangedFunction, str]]:
    """用 LLM 生成单测；失败或超约束返回空（走兜底）。"""
    try:
        payload = [
            {"file": f.file_path, "function": f.function_name, "signature": f.signature,
             "code": (f.full_source or f.source_code)[:1500]}
            for f in batch
        ]
        user = (
            f"unit_test 任务：生成单元测试\n"
            f"语言：{adapter.language}\n"
            f"请为以下函数生成单元测试代码，覆盖正常/边界/异常三类，断言稳定：\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n"
            "输出 JSON：{\"tests\": [{\"function\": 函数名, \"code\": 完整测试代码}]}"
        )
        system = "你是资深单元测试工程师，只输出 JSON。"
        raw = llm.chat_json(system, user)
        tests = raw.get("tests", []) if isinstance(raw, dict) else []
        by_name = {f.function_name: f for f in batch}
        out = []
        for t in tests:
            if isinstance(t, dict) and t.get("function") in by_name and t.get("code"):
                out.append((by_name[t["function"]], t["code"]))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 单测生成失败: %s", e)
        return []


def _execute(adapter: LanguageAdapter, codes: list[str], funcs: list[ChangedFunction] | None = None) -> dict:
    """把测试代码落盘并调用适配器执行。

    Go 语言：额外把被测函数的完整源码按原文件名拷入执行目录，
    使 go test 对变更代码做真实类型检查与编译验证（mock 模式不生成业务断言，
    但变更代码必须可编译）。
    """
    if not codes:
        return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": "没有通过语法校验的测试代码"}
    with tempfile.TemporaryDirectory(prefix="wb_tests_") as tmp:
        for i, code in enumerate(codes):
            if adapter.language == "go":
                fname = f"gen_{i}_test.go"
            elif adapter.language in ("javascript", "typescript"):
                # node:test 默认发现 *.test.js / test-*.js 等模式
                fname = f"test_{i}.test.{'js' if adapter.language == 'javascript' else 'ts'}"
            else:
                fname = f"test_gen_{i}{_ext_for(adapter.language)}"
            (Path(tmp) / fname).write_text(code, encoding="utf-8")
        if adapter.language == "go" and funcs:
            seen: set[str] = set()
            for f in funcs:
                fname = Path(f.file_path).name
                if fname in seen or not getattr(f, "full_source", ""):
                    continue
                seen.add(fname)
                (Path(tmp) / fname).write_text(f.full_source, encoding="utf-8")
        try:
            return adapter.execute_tests(tmp)
        except Exception as e:  # noqa: BLE001
            logger.exception("测试执行失败")
            return {"passed": 0, "failed": 0, "results": [], "coverage": None, "error": str(e)}


def _ext_for(language: str) -> str:
    return {"python": ".py", "javascript": ".js", "typescript": ".ts", "go": ".go"}.get(language, ".py")

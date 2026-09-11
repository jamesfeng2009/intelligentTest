"""LanguageAdapter 抽象接口 —— 多语言适配器基座。

三层架构（对齐项目计划 §4）：
- 流程统一：git diff → AST 函数定位 → 影响面 → 走查 → 单测生成 → 执行
- 数据模型统一：ChangedFunction / RiskAssessment
- 语言适配层可插拔：新增语言只需实现本接口
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChangedFunction:
    file_path: str
    function_name: str
    start_line: int
    end_line: int
    changed_lines: list[int] = field(default_factory=list)
    source_code: str = ""
    signature: str = ""
    full_source: str = ""

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "function_name": self.function_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "changed_lines": self.changed_lines,
            "signature": self.signature,
        }


class LanguageAdapter:
    """各语言适配器必须实现的接口。"""

    language: str = "base"
    # 支持的测试框架命令（用于执行阶段）
    test_command: list[str] = []

    # ---- AST 解析 ----
    def extract_functions(self, source: str) -> list[dict]:
        """提取文件中所有函数/方法：[{name, start_line, end_line}]。"""
        raise NotImplementedError

    def extract_calls(self, source: str) -> list[str]:
        """提取函数体内部调用的其他函数名（下游被调用分析用）。"""
        raise NotImplementedError

    # ---- 单测 ----
    def generate_test_code(self, func: ChangedFunction, source: str) -> str:
        """生成单元测试代码。"""
        raise NotImplementedError

    def validate_syntax(self, code: str) -> tuple[bool, str]:
        """语法检查：返回 (是否通过, 错误信息)。"""
        raise NotImplementedError

    def execute_tests(self, test_dir: str) -> dict:
        """执行测试目录下的测试，返回 {passed, failed, results, coverage}。"""
        raise NotImplementedError


@dataclass
class RiskAssessment:
    function: str
    file: str
    risk_level: str  # high / medium / low
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"function": self.function, "file": self.file,
                "risk_level": self.risk_level, "reasons": self.reasons}

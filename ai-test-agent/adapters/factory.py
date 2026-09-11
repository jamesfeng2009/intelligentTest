"""适配器工厂 —— 根据文件扩展名选择语言适配器。"""
from __future__ import annotations

from .base import LanguageAdapter
from .go_adapter import GoAdapter
from .javascript_adapter import JavaScriptAdapter
from .python_adapter import PythonAdapter
from .typescript_adapter import TypeScriptAdapter

_ADAPTERS: list[type[LanguageAdapter]] = [
    PythonAdapter,
    JavaScriptAdapter,
    TypeScriptAdapter,
    GoAdapter,
]


def get_adapter(file_path: str) -> LanguageAdapter | None:
    """按文件路径匹配适配器；不支持的语言返回 None。"""
    for cls in _ADAPTERS:
        if cls.detect(file_path):
            return cls()
    return None


def supported_extensions() -> list[str]:
    exts: list[str] = []
    for cls in _ADAPTERS:
        exts.extend(cls.EXTENSIONS)
    return exts

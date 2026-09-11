"""全局配置管理。

配置优先级：环境变量 > .env 文件 > 默认值。
所有配置以 AI_TEST_ 前缀的环境变量注入，避免与业务环境冲突。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv  # 可选依赖
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return None

# 项目根目录（ai-test-agent/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    """读取环境变量（支持 .env）。"""
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get(key, default)


@dataclass
class Settings:
    # --- LLM ---
    llm_base_url: str = field(
        default_factory=lambda: _env("AI_TEST_LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    )
    llm_api_key: str = field(default_factory=lambda: _env("AI_TEST_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("AI_TEST_LLM_MODEL", "doubao-pro-32k"))
    llm_timeout: int = field(default_factory=lambda: int(_env("AI_TEST_LLM_TIMEOUT", "60")))
    llm_max_retries: int = field(default_factory=lambda: int(_env("AI_TEST_LLM_MAX_RETRIES", "2")))
    llm_temperature: float = field(default_factory=lambda: float(_env("AI_TEST_LLM_TEMPERATURE", "0.2")))

    # 未配置 API Key 时启用 mock 模式（确定性规则生成，保证全流程可运行）
    @property
    def llm_available(self) -> bool:
        return bool(self.llm_api_key)

    # --- 路径 ---
    artifacts_dir: Path = field(
        default_factory=lambda: Path(_env("AI_TEST_ARTIFACTS", str(PROJECT_ROOT / "artifacts")))
    )

    # --- 护栏默认值 ---
    max_retry: int = field(default_factory=lambda: int(_env("AI_TEST_MAX_RETRY", "2")))
    default_timeout_s: int = field(default_factory=lambda: int(_env("AI_TEST_TIMEOUT", "60")))

    # --- 执行 ---
    ui_service_url: str = field(default_factory=lambda: _env("AI_TEST_UI_SERVICE_URL", "http://127.0.0.1:8399"))
    ui_service_enabled: bool = field(default_factory=lambda: _env("AI_TEST_UI_SERVICE_ENABLED", "false").lower() == "true")


_settings: Settings | None = None


def get_settings() -> Settings:
    """单例获取配置。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def as_dict() -> dict[str, Any]:
    """输出脱敏配置（供日志/报告使用）。"""
    s = get_settings()
    return {
        "llm_base_url": s.llm_base_url,
        "llm_model": s.llm_model,
        "llm_available": s.llm_available,
        "artifacts_dir": str(s.artifacts_dir),
        "max_retry": s.max_retry,
        "ui_service_enabled": s.ui_service_enabled,
    }

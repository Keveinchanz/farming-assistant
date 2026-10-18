"""后端运行时配置 —— 从 .env 读取，供 main.py 与各 router 使用。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.common.paths import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    max_upload_mb: int = 20

    # 业务默认值
    diagnose_default_dialect: str = "mandarin"
    confidence_threshold: float = 0.45

    # 三处能力的开关（与 src/*/factory.py 读的是同一批变量）
    voice_provider: str = "auto"
    llm_provider: str = "auto"
    rag_enabled: str = "auto"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

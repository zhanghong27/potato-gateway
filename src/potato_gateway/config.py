from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


WEAK_OR_PLACEHOLDER_TOKENS = {
    "123456",
    "password",
    "secret",
    "token",
    "your-secret-token",
    "replace-with-a-long-random-token",
    "changeme",
}


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gateway_token: str = Field(alias="POTATO_GATEWAY_TOKEN")
    host: str = Field(default="127.0.0.1", alias="POTATO_GATEWAY_HOST")
    port: int = Field(default=8765, alias="POTATO_GATEWAY_PORT")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        alias="POTATO_GATEWAY_LOG_LEVEL",
    )
    hermes_home: Path = Field(
        default_factory=lambda: Path.home() / ".hermes",
        alias="POTATO_HERMES_HOME",
    )
    agent_registry_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "config/agents.yaml",
        alias="POTATO_AGENT_REGISTRY_PATH",
    )
    calibration_state_dir: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "runtime/calibration",
        alias="POTATO_CALIBRATION_STATE_DIR",
    )
    database_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "runtime/potato-gateway.db",
        alias="POTATO_GATEWAY_DB_PATH",
    )
    hub_url: str = Field(default="http://127.0.0.1:8787", alias="POTATO_HUB_URL")
    hub_token: str = Field(default="", alias="POTATO_HUB_TOKEN")
    hub_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        le=300.0,
        alias="POTATO_HUB_TIMEOUT_SECONDS",
    )
    public_base_url: str = Field(
        default="https://zhanghongmac-mini.tail282e0b.ts.net",
        alias="POTATO_GATEWAY_PUBLIC_BASE_URL",
    )

    @field_validator("gateway_token")
    @classmethod
    def validate_gateway_token(cls, value: str) -> str:
        token = value.strip()
        if not token:
            raise ValueError("POTATO_GATEWAY_TOKEN must be configured and non-empty")
        if token.lower() in WEAK_OR_PLACEHOLDER_TOKENS:
            raise ValueError("POTATO_GATEWAY_TOKEN must not use a weak or placeholder value")
        if len(token) < 32:
            raise ValueError("POTATO_GATEWAY_TOKEN must be at least 32 characters long")
        return token

    @field_validator("hub_url")
    @classmethod
    def validate_hub_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("POTATO_HUB_URL must be an HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("POTATO_HUB_URL must not contain a path, query, or fragment")
        return value.strip().rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("POTATO_GATEWAY_PUBLIC_BASE_URL must be an HTTP(S) URL")
        return value.strip().rstrip("/")

    def resolved_hub_token(self) -> str:
        if self.hub_token:
            return self.hub_token
        token_path = (self.hermes_home / "potato-relay" / ".hub-token").resolve()
        try:
            token_path.relative_to(self.hermes_home.expanduser().resolve())
            if token_path.is_file() and token_path.stat().st_size <= 4096:
                return token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            pass
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

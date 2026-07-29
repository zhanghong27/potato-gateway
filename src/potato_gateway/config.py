from __future__ import annotations

from functools import lru_cache
from typing import Literal

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

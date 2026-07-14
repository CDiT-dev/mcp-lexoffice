"""Settings loaded from environment via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for mcp-lexoffice."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    lexoffice_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Lexoffice REST API bearer token. Injected at deploy time as a raw value.",
    )

    mcp_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Static API key for MCP bearer token auth (validated by BearerTokenVerifier).",
    )

    mcp_transport: Literal["stdio", "http", "streamable-http"] = Field(
        default="stdio",
        description="FastMCP transport mode.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached Settings accessor."""
    return Settings()

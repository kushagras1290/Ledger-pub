from functools import cached_property

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledger:ledger@localhost:5432/ledger"
    api_keys: str = Field(
        default="",
        description="Comma-separated list of accepted API keys. At least one is required.",
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_statement_timeout_ms: int = Field(default=5_000, ge=100)
    max_write_retries: int = Field(default=3, ge=1, le=10)
    log_level: str = "INFO"

    @field_validator("api_keys")
    @classmethod
    def _require_keys(cls, value: str) -> str:
        keys = [k.strip() for k in value.split(",") if k.strip()]
        if not keys:
            raise ValueError("LEDGER_API_KEYS must contain at least one key")
        if any(len(k) < 16 for k in keys):
            raise ValueError("every API key must be at least 16 characters")
        return value

    @cached_property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

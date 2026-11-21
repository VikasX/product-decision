from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mongodb_uri: SecretStr
    mongodb_database: str = Field(default="product_reviews", min_length=1)
    mongodb_timeout_ms: int = Field(default=5000, gt=0, le=60000)
    cors_origins: list[AnyHttpUrl] = []
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    openai_api_key: SecretStr | None = None
    openai_model: Literal["gpt-5-nano", "gpt-5-nano-2025-08-07"] = "gpt-5-nano"
    openai_max_output_tokens: int = Field(default=2000, ge=128, le=4000)
    openai_max_input_tokens: int = Field(default=40000, ge=1000, le=100000)
    ai_daily_budget_usd: Decimal = Field(default=Decimal("0.02"), ge=0, le=1000)
    ai_lifetime_budget_usd: Decimal = Field(default=Decimal("0.10"), ge=0, le=1000)
    ai_max_paid_attempts_per_day: int = Field(default=20, ge=0, le=1000)
    ai_state_path: Path = Path(__file__).resolve().parents[2] / ".data" / "analysis.sqlite3"

    @field_validator("mongodb_uri")
    @classmethod
    def require_mongodb_uri(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must use mongodb:// or mongodb+srv://")
        return value

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Read ignored .env values; process environment variables take precedence.
class CorsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    # Extra browser origins allowed to call FastAPI, supplied as a JSON list in the environment.
    cors_origins: list[AnyHttpUrl] = []


# Typed startup configuration rejects unsupported models and invalid limits early.
class Settings(CorsSettings):
    # Database connection secret; SecretStr hides it in ordinary object representations.
    mongodb_uri: SecretStr
    # Choose which database holds app data and shared state when MongoDB storage is enabled.
    mongodb_database: str = Field(default="product_reviews", min_length=1)
    # Bound database operations in milliseconds so unavailable storage does not hang requests.
    mongodb_timeout_ms: int = Field(default=5000, gt=0, le=60000)
    # Control how much application logging is emitted; accepted levels are restricted below.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Server-only secret; never put this value in a NEXT_PUBLIC variable.
    openai_api_key: SecretStr | None = None
    # Allow only the configured GPT-5 nano names whose pricing our budget code understands.
    openai_model: Literal["gpt-5-nano", "gpt-5-nano-2025-08-07"] = "gpt-5-nano"
    # Completion tokens include reasoning; input bounds also account for schema framing.
    # Set the completion allowance, including reasoning tokens, and validate its safe range.
    openai_max_output_tokens: int = Field(default=2000, ge=128, le=4000)
    # Set the conservative input allowance used before making either model request.
    openai_max_input_tokens: int = Field(default=40000, ge=1000, le=100000)
    # App-side conservative reservations, not the OpenAI account balance or billed usage.
    # Decimal dollars for today app cap; this is independent of the OpenAI account credit balance.
    ai_daily_budget_usd: Decimal = Field(default=Decimal("0.02"), ge=0, le=1000)
    # Decimal dollar cap across all retained reservations in the chosen store.
    ai_lifetime_budget_usd: Decimal = Field(default=Decimal("0.10"), ge=0, le=1000)
    # Also cap attempt count, even if each attempt is cheap or fails.
    ai_max_paid_attempts_per_day: int = Field(default=20, ge=0, le=1000)
    # Default local SQLite file path; existing reservations persist between app restarts.
    ai_state_path: Path = Path(__file__).resolve().parents[2] / ".data" / "analysis.sqlite3"
    # auto chooses local SQLite unless running on Vercel; mongodb explicitly enables shared state.
    ai_state_backend: Literal["auto", "sqlite", "mongodb"] = "auto"
    # Vercel sets this environment marker; it forces shared budgeting and disables local Lab access.
    vercel: bool = False
    # Requested collection size, with validation here and a stricter active cap of eight in the collector.
    amazon_max_reviews: int = Field(default=8, ge=3, le=100)
    # Legacy compatibility setting: the active product-page collector does not use it.
    amazon_max_pages: int = Field(default=7, ge=1, le=10)
    # Maximum wait for an individual Amazon HTTP operation.
    amazon_timeout_seconds: float = Field(default=10, ge=1, le=20)
    # Allow at most one extra attempt for network/temporary server errors, never access restrictions.
    amazon_transient_retries: int = Field(default=1, ge=0, le=1)
    # Enables the local endpoint; Vercel remains denied independently of this setting.
    langchain_lab_enabled: bool = False
    # Limit the compact evidence JSON before either LLM adapter sends it.
    analysis_evidence_max_bytes: int = Field(default=10000, ge=2000, le=20000)

    # Access this computed choice like a field; do not store a second value that could disagree.
    @property
    def use_mongo_state(self) -> bool:
        # Vercel instances must share durable limits regardless of an old local override.
        return self.vercel or self.ai_state_backend == "mongodb"

    # Pydantic calls this validator while loading settings, before clients are created.
    @field_validator("mongodb_uri")
    # A class method receives the model class rather than an already-created settings object.
    @classmethod
    def require_mongodb_uri(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must use mongodb:// or mongodb+srv://")
        return value

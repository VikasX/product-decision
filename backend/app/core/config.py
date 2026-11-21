from typing import Literal

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: SecretStr
    cors_origins: list[AnyHttpUrl] = []
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def require_async_postgres(cls, value: SecretStr) -> SecretStr:
        if make_url(value.get_secret_value()).drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return value

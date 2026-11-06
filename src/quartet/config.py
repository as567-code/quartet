"""Settings loaded lazily from the environment and an optional ``.env`` file."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Quartet runtime settings. Secrets are ``SecretStr`` and never appear in ``repr``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = Field(validation_alias="QUARTET_DATABASE_URL")
    admin_database_url: SecretStr | None = Field(
        default=None, validation_alias="QUARTET_ADMIN_DATABASE_URL"
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    home: Path = Field(default=Path("~/.quartet").expanduser(), validation_alias="QUARTET_HOME")
    otlp_endpoint: str = "http://127.0.0.1:4318"


def load_settings() -> Settings:
    """Build ``Settings``. Call only from commands that need them, never at import time."""
    return Settings()

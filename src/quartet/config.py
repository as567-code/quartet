"""Settings loaded lazily from the environment and an explicitly chosen env file.

A ``.env`` in the current directory is never read: running ``quartet`` inside an untrusted
repository must not let that repository redirect database or telemetry connections.
"""

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DB_SCHEMES = {"postgresql", "postgresql+psycopg"}
_DB_KEYS_ALLOWED = {
    "user",
    "password",
    "host",
    "port",
    "dbname",
    "sslmode",
    "connect_timeout",
    "application_name",
}
_OTLP_HOSTS = {"127.0.0.1", "localhost", "::1"}


class InsecureEnvFile(ValueError):  # name fixed by the controller ruling
    """The env file is missing, not a regular file, not ours, or writable by others."""


class InvalidSettings(ValueError):
    """Settings failed validation. The message names fields only, never values."""


def _check_db_url(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value()
    if "#" in raw or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("database URL contains forbidden characters")
    scheme = raw.split(":", 1)[0]
    if scheme not in _DB_SCHEMES or ":" not in raw:
        raise ValueError("database URL scheme must be postgresql or postgresql+psycopg")
    libpq_url = re.sub(r"^postgresql\+psycopg:", "postgresql:", raw)
    try:
        keys = set(conninfo_to_dict(libpq_url))
    except psycopg.Error:
        raise ValueError("not a valid database URL") from None
    extra = keys - _DB_KEYS_ALLOWED
    if extra:
        raise ValueError(
            "database URL uses disallowed connection parameters: " + ", ".join(sorted(extra))
        )
    return value


class Settings(BaseSettings):
    """Quartet runtime settings. Secrets are ``SecretStr`` and never appear in ``repr``."""

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr = Field(validation_alias="QUARTET_DATABASE_URL")
    admin_database_url: SecretStr | None = Field(
        default=None, validation_alias="QUARTET_ADMIN_DATABASE_URL"
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    home: Path = Field(
        default_factory=lambda: Path("~/.quartet").expanduser(), validation_alias="QUARTET_HOME"
    )
    otlp_endpoint: str = "http://127.0.0.1:4318"

    @field_validator("database_url", "admin_database_url")
    @classmethod
    def _validate_db_url(cls, value: SecretStr | None) -> SecretStr | None:
        return _check_db_url(value)

    @field_validator("otlp_endpoint")
    @classmethod
    def _validate_otlp(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            host = parts.hostname
        except ValueError:
            raise ValueError("not a valid OTLP endpoint") from None
        if parts.scheme not in {"http", "https"} or host not in _OTLP_HOSTS:
            raise ValueError("OTLP endpoint must be http(s) on 127.0.0.1, localhost or ::1")
        return value


def _verify_env_file(path: Path) -> None:
    try:
        st = path.stat()
    except OSError:
        raise InsecureEnvFile(f"env file {path} cannot be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise InsecureEnvFile(f"env file {path} is not a regular file")
    if st.st_uid != os.getuid():
        raise InsecureEnvFile(f"env file {path} is not owned by the current user")
    if st.st_mode & 0o022:
        raise InsecureEnvFile(f"env file {path} must not be group- or world-writable")


def _resolve_env_file(env_file: Path | None) -> Path | None:
    if env_file is not None:
        chosen: Path | None = env_file.expanduser()
    elif os.environ.get("QUARTET_ENV_FILE"):
        chosen = Path(os.environ["QUARTET_ENV_FILE"]).expanduser()
    else:
        default = Path("~/.quartet/.env").expanduser()
        chosen = default if default.exists() else None
    if chosen is not None:
        _verify_env_file(chosen)
    return chosen


def load_settings(env_file: Path | None = None) -> Settings:
    """Build ``Settings``. Call only from commands that need them, never at import time.

    Env file resolution: explicit argument, then ``QUARTET_ENV_FILE``, then
    ``~/.quartet/.env`` if present, else none.
    """
    chosen = _resolve_env_file(env_file)
    try:
        return Settings(_env_file=chosen)
    except ValidationError as exc:
        fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}))
        raise InvalidSettings(f"invalid or missing configuration: {fields}") from None

"""Environment health checks behind ``quartet doctor``."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from quartet.config import Settings, libpq_url

MIN_GIT = (2, 40)
_SUBPROCESS_TIMEOUT = 5


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def redact_url(url: str) -> str:
    """Return ``url`` with any password replaced by ``***``."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    userinfo = f"{parts.username}:***" if parts.username else ":***"
    return parts._replace(netloc=f"{userinfo}@{host}").geturl()


def _run(exe: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # argv list, absolute path from which(), no shell
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
        check=False,
    )


def _check_docker() -> Check:
    exe = shutil.which("docker")
    if exe is None:
        return Check("docker", False, "not installed")
    try:
        proc = _run(exe, ["version", "--format", "{{.Server.Version}}"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Check("docker", False, f"docker failed: {type(exc).__name__}")
    if proc.returncode != 0:
        return Check("docker", False, "daemon not reachable")
    return Check("docker", True, f"server {proc.stdout.strip()}")


def _check_git() -> Check:
    exe = shutil.which("git")
    if exe is None:
        return Check("git", False, "not installed")
    try:
        proc = _run(exe, ["--version"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Check("git", False, f"git failed: {type(exc).__name__}")
    match = re.search(r"(\d+)\.(\d+)", proc.stdout)
    if proc.returncode != 0 or match is None:
        return Check("git", False, "cannot determine version")
    version = (int(match.group(1)), int(match.group(2)))
    needed = ".".join(map(str, MIN_GIT))
    if version < MIN_GIT:
        return Check("git", False, f"{match.group(0)} < {needed}")
    return Check("git", True, match.group(0))


def _check_postgres(database_url: str) -> Check:
    url = libpq_url(database_url)
    try:
        info = conninfo_to_dict(url)
    except psycopg.Error:
        return Check("postgres", False, "invalid database URL")
    target = f"{info.get('host') or 'localhost'}:{info.get('port') or 5432}"
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except (psycopg.Error, OSError) as exc:
        return Check("postgres", False, f"cannot connect to {target}: {type(exc).__name__}")
    return Check("postgres", True, f"connected to {target}")


def _check_key(name: str, value: SecretStr | None) -> Check:
    present = value is not None and value.get_secret_value() != ""
    return Check(name, present, "present" if present else "missing")


def run_checks(settings: Settings) -> list[Check]:
    return [
        _check_docker(),
        _check_git(),
        _check_postgres(settings.database_url.get_secret_value()),
        _check_key("anthropic_key", settings.anthropic_api_key),
        _check_key("openai_key", settings.openai_api_key),
    ]

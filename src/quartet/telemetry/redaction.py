"""Secret redaction for log records, exception messages and span descriptions."""

import logging
import re
from collections.abc import Iterable
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from quartet.config import Settings, libpq_url

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8

# (compiled pattern, replacement); applied in this order after the exact secret values.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"), REDACTED),
    (
        re.compile(
            r"(?i)\b(x-api-key|api[_-]?key|authorization)([\"']?\s*[:=]\s*[\"']?)[^\s\"',]+"
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(r"(postgres(?:ql)?(?:\+psycopg)?://[^:/\s@]+:)[^@\s]+@"),
        rf"\1{REDACTED}@",
    ),
)


class Redactor:
    """Replaces known secret values, then well-known secret shapes, with ``REDACTED``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}
        # Longest first so a secret that contains another is replaced whole.
        self._secrets = sorted(values, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def redactor_from_settings(settings: Settings) -> Redactor:
    """Redactor for the configured API keys and the passwords inside the database URLs."""
    secrets: list[str] = []
    for key in (settings.anthropic_api_key, settings.openai_api_key):
        if key is not None:
            secrets.append(key.get_secret_value())
    for url in (
        settings.database_url,
        settings.admin_database_url,
        settings.migrator_database_url,
    ):
        if url is None:
            continue
        password = conninfo_to_dict(libpq_url(url.get_secret_value())).get("password")
        if password:
            secrets.append(str(password))
    return Redactor(secrets)


def redact_value(value: Any, redactor: Redactor) -> Any:
    """Redact every string inside ``value``, recursing through dicts and lists."""
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, redactor) for v in value]
    return value


class RedactingFilter(logging.Filter):
    """Handler-level filter that redacts the rendered message of each record."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = ()
        return True


_SAFE_ERRORS = (TypeError, ValueError, KeyError, IndexError, AttributeError)
_factory_state: dict[str, Any] = {"installed": False, "redactor": Redactor()}


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except _SAFE_ERRORS:
        return "<unprintable>"


def _redacted_message(record: logging.LogRecord, redactor: Redactor) -> str:
    try:
        return redactor.redact(record.getMessage())
    except _SAFE_ERRORS:
        # A bad format string: log.info("n=%d", "text") used to be swallowed by Handler.handleError.
        args = record.args
        if isinstance(args, tuple):
            parts: tuple[object, ...] = args
        elif args:
            parts = (args,)
        else:
            parts = ()
        return " ".join(redactor.redact(_safe_str(x)) for x in (record.msg, *parts))


def install_log_redaction(redactor: Redactor) -> None:
    """Redact every log record at creation, whichever logger emitted it.

    A filter on a logger never sees records propagated from child loggers, so the hook is the
    record factory. Idempotent: later calls only swap the redactor in use.
    """
    _factory_state["redactor"] = redactor
    if _factory_state["installed"]:
        return
    inner = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = inner(*args, **kwargs)
        record.msg = _redacted_message(record, _factory_state["redactor"])
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)
    _factory_state["installed"] = True

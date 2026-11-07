"""Secret redaction for log records, exception messages and span descriptions."""

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

from psycopg.conninfo import conninfo_to_dict

from quartet.config import Settings, libpq_url

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8

# Left edge of an sk- key: not mid-word, but still after an escaped newline or a %XX escape.
_SK_LEFT = r"(?:(?<![A-Za-z0-9])|(?<=\\[nrt])|(?<=%[0-9A-Fa-f]{2}))"

_SCHEME = r"(?:basic|bearer|token|digest|apikey|negotiate|ntlm|ssws)"
# A quoted value with backslash escapes; the closing quote may be missing (unterminated).
_QUOTED = r"""(?:(?P<dq>")(?:\\.|[^"\\\r\n])*(?P<dqc>"?)|(?P<sq>')(?:\\.|[^'\\\r\n])*(?P<sqc>'?))"""


def _keep_quotes(m: re.Match[str]) -> str:
    """Replace a matched value with REDACTED, keeping the quote characters around it."""
    opening = m.group("dq") or m.group("sq") or ""
    closing = m.group("dqc") or m.group("sqc") or ""
    return f"{m.group(1)}{m.group(2)}{opening}{REDACTED}{closing}"


# (compiled pattern, replacement); applied in this order after the exact secret values.
PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (re.compile(_SK_LEFT + r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(_SK_LEFT + r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"), REDACTED),
    # 6a: whole Authorization header value (any scheme): a quoted value through its closing quote
    # (escapes honoured), else to end of line or the first quote.
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(authorization)([\"']?\s*[:=]\s*)"
            rf"(?:(?:{_SCHEME}\s+)?{_QUOTED}|[^\r\n\"']+)"
        ),
        _keep_quotes,
    ),
    # 6b: key and password fields. VALUE := QUOTED | (SCHEME ws)? (QUOTED | TOKEN); the quotes of a
    # quoted value are kept around the redaction.
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(x-api-key|api[_-]?key|password|passwd)([\"']?\s*[:=]\s*)"
            rf"(?:{_SCHEME}\s+)?(?:{_QUOTED}|[^\s\"',]+)"
        ),
        _keep_quotes,
    ),
    (
        re.compile(r"(postgres(?:ql)?(?:\+psycopg)?://[^:/\s@]+:)[^@\s]+@"),
        rf"\1{REDACTED}@",
    ),
)


class Redactor:
    """Replaces known secret values, then well-known secret shapes, with ``REDACTED``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values: set[str] = set()
        for s in secrets:
            if len(s) >= _MIN_SECRET_LENGTH:
                values.add(s)
                values.add(quote(s, safe=""))  # percent-encoded form, as inside a URL
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
        _redact_record(record, self._redactor)
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


def _redact_record(record: logging.LogRecord, redactor: Redactor) -> None:
    record.msg = _redacted_message(record, redactor)
    record.args = ()
    if record.exc_info:
        # Render now, redacted: formatters would otherwise print the raw traceback later.
        record.exc_text = redactor.redact(logging.Formatter().formatException(record.exc_info))
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = redactor.redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redactor.redact(record.stack_info)


def install_log_redaction(redactor: Redactor) -> None:
    """Redact every log record at creation, whichever logger emitted it.

    A filter on a logger never sees records propagated from child loggers, so the hook is the
    record factory. Idempotent: later calls only swap the redactor in use. An exception raised
    by a user ``__str__`` outside the caught error types may still propagate.
    """
    _factory_state["redactor"] = redactor
    if _factory_state["installed"]:
        return
    inner = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = inner(*args, **kwargs)
        _redact_record(record, _factory_state["redactor"])
        return record

    logging.setLogRecordFactory(factory)
    _factory_state["installed"] = True

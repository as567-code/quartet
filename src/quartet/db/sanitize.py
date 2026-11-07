"""Making untrusted text safe to store: no NUL, no lone surrogates, no non-finite numbers.

Postgres ``text`` and ``jsonb`` reject NUL (``\\u0000``) and strings that are not valid UTF-8,
and ``jsonb`` rejects ``NaN`` and ``Infinity``. A rejected write raises a driver error whose
message quotes the row, so untrusted text is cleaned here before it reaches a statement.
"""

import math
import re
from typing import Any

REPLACEMENT = "�"
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def strip_nul(text: str) -> str:
    return text.replace("\x00", "")


def sanitize_text(text: str) -> str:
    """NUL removed, each lone surrogate replaced by U+FFFD."""
    return _LONE_SURROGATE.sub(REPLACEMENT, strip_nul(text))


def sanitize_for_storage(value: Any) -> Any:
    """``sanitize_text`` over every string in ``value``, dict keys included.

    Recurses through dicts and lists. A non-finite float becomes ``None``. Other values pass
    through unchanged.
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_for_storage(k): sanitize_for_storage(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize_for_storage(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value

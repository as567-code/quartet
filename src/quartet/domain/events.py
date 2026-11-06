"""Audit-event payload guard shared by every writer of the ``events`` table."""

import json
from collections.abc import Mapping
from typing import Any

EVENT_PAYLOAD_MAX_BYTES = 65_536


def check_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``payload`` as a plain dict, or raise ``ValueError`` if it is too large.

    The size is the UTF-8 byte length of the compact JSON encoding. Error messages carry the
    size and the limit only, never payload content.
    """
    data = dict(payload)
    try:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("event payload is not valid UTF-8 text") from None
    if len(encoded) > EVENT_PAYLOAD_MAX_BYTES:
        raise ValueError(
            f"event payload is {len(encoded)} bytes; the limit is {EVENT_PAYLOAD_MAX_BYTES} bytes"
        )
    return data

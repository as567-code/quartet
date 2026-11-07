import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from quartet.telemetry.redaction import Redactor

CASSETTES = Path(__file__).resolve().parents[2] / "tests/fixtures/cassettes"
FORBIDDEN = re.compile(
    r"(?i)\b(authorization|x-api-key|cookie|set-cookie|openai-organization|openai-project)\b"
)


def _strings(value: Any) -> Iterator[str]:
    """Every decoded string inside ``value``, recursing through dicts and lists."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_cassettes_hold_no_credentials() -> None:
    # Vacuous until Task 15 records real cassettes; the glob then covers every one.
    for f in CASSETTES.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        headers = [e["response_headers"] for e in data["exchanges"]]
        assert not FORBIDDEN.search(json.dumps(headers)), f.name
        for exchange in data["exchanges"]:
            for value in _strings(exchange):
                assert Redactor().redact(value) == value, f.name
            # Key/value pairs inside the request body are only visible as a whole structure.
            body = json.dumps(exchange["request_body"])
            assert Redactor().redact(body) == body, f.name

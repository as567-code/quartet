import json
import re
from pathlib import Path

from quartet.telemetry.redaction import Redactor

CASSETTES = Path(__file__).resolve().parents[2] / "tests/fixtures/cassettes"
FORBIDDEN = re.compile(
    r"(?i)\b(authorization|x-api-key|cookie|set-cookie|openai-organization|openai-project)\b"
)


def test_cassettes_hold_no_credentials() -> None:
    # Vacuous until Task 15 records real cassettes; the glob then covers every one.
    for f in CASSETTES.glob("*.json"):
        text = f.read_text()
        headers = [e["response_headers"] for e in json.loads(text)["exchanges"]]
        assert not FORBIDDEN.search(json.dumps(headers)), f.name
        assert Redactor().redact(text) == text, f.name

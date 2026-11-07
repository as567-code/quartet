"""Record real HTTP exchanges to scrubbed JSON cassettes, and replay them offline.

Only the response headers in ``RESPONSE_HEADERS_KEPT`` are stored, and request headers never
are. Bodies and paths are scrubbed by the ``Redactor`` when a cassette is saved, and the saved
text is re-checked against ``PATTERNS`` before anything is written.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from quartet.telemetry.redaction import PATTERNS, Redactor, redact_value

CASSETTE_VERSION = 1
RESPONSE_HEADERS_KEPT = frozenset({"content-type", "request-id", "x-request-id"})


@dataclass(frozen=True)
class Exchange:
    method: str
    path: str  # includes the query string
    request_body: Any
    status: int
    response_headers: Mapping[str, str]
    response_body: str


class CassetteMismatch(AssertionError):
    """A request does not match the next recorded exchange."""


class CassetteExhausted(AssertionError):
    """A request arrived after every recorded exchange was replayed."""


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


class RecordingTransport(httpx2.AsyncBaseTransport):
    """Forwards each request, stores a scrubbed ``Exchange``, and returns the same response."""

    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx2.AsyncHTTPTransport()
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        response = await self._inner.handle_async_request(request)
        try:
            payload = await response.aread()
        finally:
            await response.aclose()
        kept = {
            k.lower(): v for k, v in response.headers.items() if k.lower() in RESPONSE_HEADERS_KEPT
        }
        try:
            response_body = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("cassette bodies must be UTF-8 text") from None
        self.exchanges.append(
            Exchange(
                method=request.method,
                path=request.url.raw_path.decode("utf-8"),
                request_body=_parse_body(body),
                status=response.status_code,
                response_headers=kept,
                response_body=response_body,
            )
        )
        return httpx2.Response(response.status_code, headers=kept, content=payload, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class ReplayTransport(httpx2.AsyncBaseTransport):
    """Serves recorded exchanges strictly in order; a mismatch does not consume one."""

    def __init__(self, exchanges: Sequence[Exchange]) -> None:
        self._exchanges = list(exchanges)
        self._next = 0

    @property
    def remaining(self) -> int:
        return len(self._exchanges) - self._next

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self._next >= len(self._exchanges):
            raise CassetteExhausted(
                f"all {len(self._exchanges)} recorded exchanges were already replayed"
            )
        expected = self._exchanges[self._next]
        # The message names the recorded exchange only: the live request is not echoed.
        if (
            request.method != expected.method
            or request.url.raw_path.decode("utf-8") != expected.path
        ):
            raise CassetteMismatch(
                f"request #{self._next + 1} does not match the recorded "
                f"{expected.method} {expected.path}"
            )
        self._next += 1
        return httpx2.Response(
            expected.status,
            headers=dict(expected.response_headers),
            content=expected.response_body.encode("utf-8"),
            request=request,
        )


def _matched_pattern(text: str) -> int | None:
    """Index of the first ``PATTERNS`` entry that still changes ``text``, or None.

    An entry that re-applies its own replacement without change (a key/value pair whose value
    is already ``[REDACTED]``) is not a match.
    """
    for index, (pattern, replacement) in enumerate(PATTERNS):
        if pattern.sub(replacement, text) != text:
            return index
    return None


def _strings(value: Any) -> Iterator[str]:
    """Every string inside ``value``, recursing through dicts and lists."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def save_cassette(path: Path, exchanges: Sequence[Exchange], redactor: Redactor) -> None:
    """Write a scrubbed cassette, or raise ``ValueError`` and write nothing.

    Refused: a response header outside ``RESPONSE_HEADERS_KEPT``, or any decoded string value
    (path, request-body strings, the request body as a whole, response body, header values)
    that a ``PATTERNS`` entry still matches after redaction. The check runs on decoded values
    because JSON escaping of quotes would hide key/value patterns in the serialised text.
    """
    records: list[dict[str, Any]] = []
    for i, ex in enumerate(exchanges):
        for name in ex.response_headers:
            if name.lower() not in RESPONSE_HEADERS_KEPT:
                raise ValueError(
                    f"exchange {i}: response header {name.lower()!r} is not on the allowlist; "
                    "nothing written"
                )
        record: dict[str, Any] = {
            "method": ex.method,
            "path": redactor.redact(ex.path),
            "request_body": redact_value(ex.request_body, redactor),
            "status": ex.status,
            "response_headers": {k: redactor.redact(v) for k, v in ex.response_headers.items()},
            "response_body": redactor.redact(ex.response_body),
        }
        checks: list[tuple[str, str]] = [
            ("path", record["path"]),
            ("response_body", record["response_body"]),
            *(("request_body string", s) for s in _strings(record["request_body"])),
            # The whole structure, so a dict key and its value are seen together.
            ("request_body", json.dumps(record["request_body"])),
            *((f"response header {k}", v) for k, v in record["response_headers"].items()),
        ]
        for label, text in checks:
            index = _matched_pattern(text)
            if index is not None:
                raise ValueError(
                    f"exchange {i}: {label} still matches secret pattern #{index} after "
                    "redaction; nothing written"
                )
        records.append(record)
    text = (
        json.dumps({"version": CASSETTE_VERSION, "exchanges": records}, sort_keys=True, indent=1)
        + "\n"
    )
    path.write_text(text, encoding="utf-8")


def load_cassette(path: Path) -> list[Exchange]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != CASSETTE_VERSION:
        raise ValueError(f"unsupported cassette version: {data.get('version')!r}")
    return [
        Exchange(
            method=e["method"],
            path=e["path"],
            request_body=e["request_body"],
            status=e["status"],
            response_headers=e["response_headers"],
            response_body=e["response_body"],
        )
        for e in data["exchanges"]
    ]

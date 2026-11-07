"""A streamed response that breaks part-way through its body."""

import httpx2


class BrokenStream(httpx2.AsyncByteStream):
    """Yields the first half of ``body``, then raises ``error`` (as a dropped connection would)."""

    def __init__(self, body: bytes, error: Exception) -> None:
        self._body = body
        self._error = error

    async def __aiter__(self):
        yield self._body[: len(self._body) // 2]
        raise self._error


def broken_response(body: bytes, error: Exception) -> httpx2.Response:
    headers = {
        "content-type": "text/event-stream",
        "request-id": "req_1",
        "x-request-id": "req_1",
    }
    return httpx2.Response(200, headers=headers, stream=BrokenStream(body, error))

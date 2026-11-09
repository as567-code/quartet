"""Builders for SDK exceptions (the SDKs' HTTP types come from httpx2, not httpx)."""

from typing import Any

import httpx2


def status_error(
    cls: Any,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
    message: str = "provider said no",
) -> Any:
    req = httpx2.Request("POST", "https://api.example.test/v1/x")
    return cls(
        message,
        response=httpx2.Response(status, headers=headers or {}, request=req),
        body=body,
    )

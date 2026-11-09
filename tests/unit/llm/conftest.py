import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from anthropic import DefaultAsyncHttpxClient
from openai.types.responses import Response

from quartet.domain.artifacts import strictify_schema
from quartet.llm.base import RoleConfig, ToolSpec
from quartet.llm.prompting import PromptParts
from tests.unit.llm._records import cfg
from tests.unit.llm._sse import anthropic_sse, openai_sse

PROMPT = PromptParts("SYSTEM-TEXT", "USER-TEXT")


def tool(name: str) -> ToolSpec:
    schema = strictify_schema(
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    )
    return ToolSpec(name, f"The {name} tool", schema)


READ_FILE = tool("read_file")
ECHO = tool("echo")

# Request path -> encoder for dict responses.
_ENCODERS: dict[str, Callable[[Any], bytes]] = {
    "/v1/messages": anthropic_sse,
    "/v1/responses": openai_sse,
}


class Capture:
    """Records request bodies and headers and replays queued responses over a MockTransport."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._responses: list[Any] = []

    def _respond(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        response = self._responses.pop(0)
        if isinstance(response, httpx2.Response):
            return response
        if isinstance(response, dict | Response):
            sse = {
                "content-type": "text/event-stream",
                "request-id": "req_1",
                "x-request-id": "req_1",
            }
            return httpx2.Response(200, headers=sse, content=_ENCODERS[request.url.path](response))
        return httpx2.Response(200, content=response)

    def client(self, *responses: Any) -> httpx2.AsyncClient:
        self._responses.extend(responses)
        return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._respond))

    def openai_client(self, *responses: Any) -> httpx2.AsyncClient:
        self._responses.extend(responses)
        return openai.DefaultAsyncHttpx2Client(transport=httpx2.MockTransport(self._respond))


@pytest.fixture
def capture() -> Capture:
    return Capture()


@pytest.fixture
def role_config() -> RoleConfig:
    return cfg()

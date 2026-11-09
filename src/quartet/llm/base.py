"""Provider-neutral types, protocols and errors for the LLM layer.

Nothing here imports a vendor SDK. Error messages are built from counts, names and
categories only; free text reaches an exception only through ``ProviderError``, whose caller
passes already-redacted text (FR-30).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel

from quartet.domain.artifacts import ARTIFACT_MODELS, strictify_schema
from quartet.domain.states import Role


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

# P2-R4: FR-15's "retried at most twice" lives in the runner.
MAX_ARTIFACT_REPAIRS = 2

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class StepLimits:
    max_turns: int
    max_tool_calls: int
    max_total_tokens: int
    max_usd: Decimal
    max_wall_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.max_usd, Decimal):
            raise TypeError("max_usd must be a Decimal")
        for name in (
            "max_turns",
            "max_tool_calls",
            "max_total_tokens",
            "max_usd",
            "max_wall_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")


@dataclass(frozen=True, slots=True)
class RoleConfig:
    role: Role
    provider: Provider
    model: str
    effort: Effort
    system_prompt: str
    output_model: type[BaseModel]
    max_output_tokens: int
    limits: StepLimits
    server_fallback: bool

    def __post_init__(self) -> None:
        if self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {sorted(EFFORTS)}")
        if self.output_model not in ARTIFACT_MODELS:
            raise ValueError("output_model must be one of ARTIFACT_MODELS")
        if not self.system_prompt:
            raise ValueError("system_prompt must not be empty")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be > 0")
        if self.server_fallback and self.provider is Provider.OPENAI:
            raise ValueError("server_fallback is not available on OpenAI")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("tool name must match ^[A-Za-z0-9_-]{1,64}$")
        schema_ok = True
        try:
            Draft202012Validator.check_schema(dict(self.input_schema))
        except SchemaError:
            schema_ok = False  # raised below: the SchemaError text would quote schema content
        if not schema_ok:
            raise ValueError(f"tool input schema is not a valid JSON Schema: {self.name}")
        if dict(self.input_schema) != strictify_schema(self.input_schema):
            raise ValueError(f"tool input schema is not strict: {self.name}")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str
    is_error: bool


class Toolbox(Protocol):
    @property
    def specs(self) -> Sequence[ToolSpec]: ...

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome: ...


class _EmptyToolbox:
    specs: Sequence[ToolSpec] = ()

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        return ToolOutcome("unknown tool", True)


EMPTY_TOOLBOX: Toolbox = _EmptyToolbox()


@dataclass(frozen=True, slots=True)
class RoleInput:
    instructions: str
    context: tuple[BaseModel, ...]
    task_text_untrusted: str


@dataclass(frozen=True, slots=True)
class Usage:
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_short_tokens: int
    cache_write_long_tokens: int
    output_tokens: int
    # A subset of output_tokens: never added to it.
    reasoning_tokens: int

    @property
    def prompt_tokens(self) -> int:
        return (
            self.uncached_input_tokens
            + self.cache_read_tokens
            + self.cache_write_short_tokens
            + self.cache_write_long_tokens
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.uncached_input_tokens + other.uncached_input_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_short_tokens + other.cache_write_short_tokens,
            self.cache_write_long_tokens + other.cache_write_long_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def zero(cls) -> "Usage":
        return cls(0, 0, 0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class UsagePart:
    """One priced unit: usage attributed to the model that served it."""

    model: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class CallRecord:
    provider: Provider
    requested_model: str
    model: str
    usage: Usage
    cost_usd: Decimal
    cost_estimated: bool
    latency_ms: int
    stop_reason: str
    request_id: str | None
    trace_id: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    tool: str
    call_id: str
    args_redacted: dict[str, Any]
    is_error: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class StepTotals:
    turns: int
    tool_calls: int
    usage: Usage
    cost_usd: Decimal
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class RoleResult:
    artifact: BaseModel
    calls: tuple[CallRecord, ...]
    totals: StepTotals
    repairs: int
    trace_id: str | None


class StepRecorder(Protocol):
    async def llm_call(self, record: CallRecord) -> None: ...

    async def tool_call(self, record: ToolCallRecord) -> None: ...


class _NullRecorder:
    async def llm_call(self, record: CallRecord) -> None:
        return None

    async def tool_call(self, record: ToolCallRecord) -> None:
        return None


NULL_RECORDER: StepRecorder = _NullRecorder()


class RoleRunner(Protocol):
    async def run(
        self,
        config: RoleConfig,
        handover_input: RoleInput,
        tools: Toolbox,
        *,
        recorder: StepRecorder = ...,
    ) -> RoleResult: ...


class LLMError(Exception):
    """Base of the layer's errors; the loop sets ``totals`` and ``calls`` before raising."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.totals: StepTotals | None = None
        self.calls: tuple[CallRecord, ...] = ()

    @property
    def summary(self) -> str:
        """Content-free text for span descriptions and CLI output (this message holds no text)."""
        return str(self)


class BudgetExceeded(LLMError):
    def __init__(
        self,
        kind: Literal["turns", "tool_calls", "tokens", "usd", "wall_clock"],
        used: float | int | Decimal,
        limit: float | int | Decimal,
    ) -> None:
        super().__init__(f"step budget exceeded: {kind} ({used} of {limit})")
        self.kind = kind
        self.used = used
        self.limit = limit


class ArtifactInvalid(LLMError):
    def __init__(self, *, attempts: int, problems: tuple[str, ...]) -> None:
        super().__init__(f"artifact invalid after {attempts} attempts: {len(problems)} problems")
        self.attempts = attempts
        self.problems = problems


class ModelRefused(LLMError):
    def __init__(self, *, provider: Provider, category: str | None) -> None:
        super().__init__(f"{provider} refused (category: {category})")
        self.provider = provider
        self.category = category


class ProviderError(LLMError):
    """A vendor failure. ``message`` must already be redacted by the caller."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        turn: object | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.code = code
        self.request_id = request_id
        # The ProviderTurn (Task 8) whose usage must still be billed.
        self.turn = turn

    @property
    def summary(self) -> str:
        """Class, status and code only: the message may hold redacted vendor text."""
        return f"{type(self).__name__} (status {self.status}, code {self.code})"


class PricingUnavailable(LLMError):
    def __init__(self, *, model: str) -> None:
        super().__init__(f"no price for model {model}")
        self.model = model


class RecorderError(LLMError):
    """The ledger write failed. The message carries the SQLSTATE only, never row content."""

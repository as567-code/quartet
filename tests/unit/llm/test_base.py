import dataclasses
from typing import Any

import pytest

from quartet.domain.artifacts import Finding
from quartet.llm.base import (
    EMPTY_TOOLBOX,
    NULL_RECORDER,
    ArtifactInvalid,
    BudgetExceeded,
    LLMError,
    ModelRefused,
    PricingUnavailable,
    Provider,
    ProviderError,
    ToolSpec,
    Usage,
)
from tests.unit.llm._records import cfg, limits

pytestmark = pytest.mark.anyio


def test_step_limits_reject_non_positive_and_float_usd():
    with pytest.raises(ValueError):
        limits(max_turns=0)
    with pytest.raises(TypeError):
        limits(max_usd=1.0)


BAD_CONFIGS: list[dict[str, Any]] = [
    {"effort": "none"},
    {"output_model": Finding},
    {"system_prompt": ""},
    {"max_output_tokens": 0},
    {"provider": Provider.OPENAI, "server_fallback": True},
]


def test_role_config_validation():
    cfg()
    for bad in BAD_CONFIGS:
        with pytest.raises(ValueError):
            dataclasses.replace(cfg(), **bad)


def test_tool_spec_requires_strict_schema_and_safe_name():
    strict = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    ToolSpec("read_file", "Read a file", strict)
    with pytest.raises(ValueError, match="strict"):
        ToolSpec(
            "read_file",
            "Read a file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
        )
    with pytest.raises(ValueError):
        ToolSpec("read file", "Read a file", strict)


def test_usage_arithmetic():
    u = Usage(10, 20, 3, 4, 5, 2)
    assert u.prompt_tokens == 37
    assert (u + u).output_tokens == 10 and Usage.zero().prompt_tokens == 0
    assert (u + u).reasoning_tokens == 4


def test_artifact_invalid_message_has_counts_not_content():
    e = ArtifactInvalid(attempts=3, problems=("summary: string_type",))
    assert "3" in str(e) and e.problems == ("summary: string_type",)
    assert str(e) == "artifact invalid after 3 attempts: 1 problems"


def test_error_messages_and_defaults():
    b = BudgetExceeded("usd", 2, 1)
    assert str(b) == "step budget exceeded: usd (2 of 1)"
    assert isinstance(b, LLMError) and b.totals is None and b.calls == ()
    assert (
        str(ModelRefused(provider=Provider.OPENAI, category=None))
        == "openai refused (category: None)"
    )
    assert str(PricingUnavailable(model="m")) == "no price for model m"
    pe = ProviderError("boom", retryable=True, status=529)
    assert str(pe) == "boom" and pe.retryable and pe.status == 529 and pe.turn is None
    assert pe.code is None and pe.request_id is None


async def test_null_recorder_and_empty_toolbox():
    assert list(EMPTY_TOOLBOX.specs) == []
    await NULL_RECORDER.llm_call(None)  # type: ignore[arg-type]
    out = await EMPTY_TOOLBOX.call("x", {})
    assert out.is_error

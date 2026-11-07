from collections.abc import Iterator
from copy import deepcopy
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quartet.domain.artifacts import (
    ARTIFACT_KINDS,
    ARTIFACT_MODELS,
    ContextDigest,
    Finding,
    Plan,
    ReviewReport,
    TestReport,
    _strictify,
    count_union_params,
    strict_json_schema,
    strictify_schema,
)

FORBIDDEN = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "pattern",
    "minItems",
    "maxItems",
}


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _report(**over: Any) -> TestReport:
    base = {
        "schema_version": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failing_tests": [],
        "new_tests": [],
        "suite_command": ["pytest"],
        "output_excerpt": "x",
        "post_rebase": False,
    }
    return TestReport.model_validate({**base, **over})


@pytest.mark.parametrize("model", [*ARTIFACT_KINDS.values(), ContextDigest])
def test_schema_is_strict_for_both_vendors(model):
    schema = strict_json_schema(model)
    objects = [n for n in _walk(schema) if isinstance(n, dict) and n.get("type") == "object"]
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    keys = {k for n in _walk(schema) if isinstance(n, dict) for k in n}
    assert not (FORBIDDEN & keys)


def test_schema_covers_ref_items_in_nested_defs():
    schema = strict_json_schema(ReviewReport)
    finding = schema["$defs"]["Finding"]
    assert finding["additionalProperties"] is False
    assert set(finding["required"]) == {"file", "line", "severity", "rationale", "suggestion"}


def _nullable_and_required(obj: dict[str, Any], name: str) -> None:
    prop = obj["properties"][name]
    assert {"type": "null"} in prop["anyOf"]
    assert name in obj["required"]


def test_nullable_fields_are_anyof_null_and_required():
    _nullable_and_required(strict_json_schema(Plan), "out_of_scope_reason")
    finding = strict_json_schema(ReviewReport)["$defs"]["Finding"]
    _nullable_and_required(finding, "line")
    _nullable_and_required(finding, "suggestion")


@pytest.mark.parametrize("model", [*ARTIFACT_KINDS.values(), ContextDigest])
def test_no_default_key_anywhere_in_strict_schema(model):
    schema = strict_json_schema(model)
    assert not [n for n in _walk(schema) if isinstance(n, dict) and "default" in n]


def test_schema_version_const_shape():
    prop = strict_json_schema(Plan)["properties"]["schema_version"]
    assert prop["const"] == 1 and prop["type"] == "integer"


def test_strict_schema_returns_new_dict_without_mutating_cache():
    before = deepcopy(ReviewReport.model_json_schema())
    first = strict_json_schema(ReviewReport)
    first["$defs"]["Finding"]["additionalProperties"] = True
    first["properties"].clear()
    again = strict_json_schema(ReviewReport)
    assert again["$defs"]["Finding"]["additionalProperties"] is False
    assert again["properties"]
    assert ReviewReport.model_json_schema() == before


def test_nested_object_inside_anyof_is_strictified():
    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: int

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Inner | None

    schema = strict_json_schema(Outer)
    assert schema["$defs"]["Inner"]["additionalProperties"] is False

    obj: dict[str, Any] = {"type": "object", "properties": {"x": {"type": "integer"}}}
    raw = {"anyOf": [obj, {"type": "null"}]}
    out = _strictify(raw)
    assert out["anyOf"][0]["additionalProperties"] is False
    assert out["anyOf"][0]["required"] == ["x"]
    assert "additionalProperties" not in raw["anyOf"][0]


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ReviewReport.model_validate(
            {
                "schema_version": 1,
                "verdict": "approve",
                "findings": [],
                "injection_suspected": False,
                "merge_now": True,
            }
        )


def test_optional_fields_still_required():
    with pytest.raises(ValidationError):  # out_of_scope_reason omitted
        Plan.model_validate(
            {
                "schema_version": 1,
                "summary": "s",
                "steps": [],
                "predicted_files": [],
                "acceptance_criteria": [],
                "test_strategy": "t",
                "risk_level": "low",
            }
        )


def test_wrong_schema_version_rejected(sample_plan):
    with pytest.raises(ValidationError):
        Plan.model_validate({**sample_plan.model_dump(), "schema_version": 2})


def test_models_are_frozen(sample_plan):
    with pytest.raises(ValidationError):
        sample_plan.summary = "changed"


def test_finding_round_trip():
    f = Finding(file="a.py", line=None, severity="nit", rationale="r", suggestion=None)
    assert Finding.model_validate(f.model_dump()) == f


def test_output_excerpt_truncated_not_rejected():
    r = _report(output_excerpt="x" * 20_000)
    assert len(r.output_excerpt.encode()) <= 8 * 1024 and r.output_excerpt.endswith("…[truncated]")


def test_output_excerpt_within_cap_unchanged():
    exact = "y" * 8192
    assert _report(output_excerpt=exact).output_excerpt == exact
    assert _report(output_excerpt="short").output_excerpt == "short"


@pytest.mark.parametrize("ch", ["é", "€", "😀"])
def test_output_excerpt_multibyte_truncation_is_valid_utf8(ch):
    r = _report(output_excerpt=ch * 10_000)
    encoded = r.output_excerpt.encode("utf-8")  # raises if lone surrogate
    assert len(encoded) <= 8192
    assert r.output_excerpt.endswith("…[truncated]")
    assert encoded.decode("utf-8") == r.output_excerpt


def test_text_cap_rejects_over_16kib_bytes(sample_plan):
    ok = {**sample_plan.model_dump(), "summary": "a" * 16_384}
    assert Plan.model_validate(ok).summary == "a" * 16_384
    with pytest.raises(ValidationError):
        Plan.model_validate({**ok, "summary": "a" * 16_385})


def test_text_cap_counts_bytes_not_characters(sample_plan):
    # 8193 two-byte chars = 16386 bytes but only 8193 characters
    with pytest.raises(ValidationError):
        Plan.model_validate({**sample_plan.model_dump(), "summary": "é" * 8_193})


def test_cap_applies_to_list_items(sample_plan):
    bad = {**sample_plan.model_dump(), "acceptance_criteria": ["a" * (16 * 1024 + 1)]}
    with pytest.raises(ValidationError):
        Plan.model_validate(bad)
    ok = {**sample_plan.model_dump(), "acceptance_criteria": ["a" * (16 * 1024)]}
    assert Plan.model_validate(ok)


def test_cap_applies_to_nested_model_strings(sample_plan):
    data = sample_plan.model_dump()
    data["steps"] = [{"description": "d", "files": ["f" * 16_385]}]
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


def test_strict_schema_rejects_constrained_models():
    class Bad(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: Annotated[str, Field(min_length=1)]

    with pytest.raises(ValueError, match="minLength"):
        strict_json_schema(Bad)


def test_strict_schema_rejects_ref_cycles():
    class Node(BaseModel):
        model_config = ConfigDict(extra="forbid")
        child: "Node | None"

    Node.model_rebuild()
    with pytest.raises(ValueError, match="cycle"):
        strict_json_schema(Node)


def test_artifact_models_cover_kinds_and_digest():
    assert (*ARTIFACT_KINDS.values(), ContextDigest) == ARTIFACT_MODELS


def test_strictify_schema_is_idempotent_on_artifact_schemas():
    for model in ARTIFACT_MODELS:
        s = strict_json_schema(model)
        assert strictify_schema(s) == s


def test_union_param_counts():  # Anthropic strict budget: <= 16 unions per request (Task 9)
    assert count_union_params(strict_json_schema(ReviewReport)) == 2  # Finding.line, .suggestion
    assert count_union_params(strict_json_schema(Plan)) == 1  # out_of_scope_reason
    assert max(count_union_params(strict_json_schema(m)) for m in ARTIFACT_MODELS) <= 2

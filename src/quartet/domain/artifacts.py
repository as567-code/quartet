"""Versioned hand-off artifacts exchanged between the Quartet agents.

Every model is frozen, forbids extras and has **no field defaults** so that
:func:`strict_json_schema` yields a schema acceptable to both the Anthropic (structured
outputs / strict tools) and OpenAI (Responses API strict ``json_schema``) APIs: every object
has ``additionalProperties: false`` and lists all of its properties as required; optional
values are ``T | None`` and stay required.

Size caps are enforced by validators (never by ``Field`` constraints, which would add
unsupported keywords such as ``maxLength`` to the schema).
"""

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

TEXT_CAP_BYTES = 16 * 1024
EXCERPT_CAP_BYTES = 8 * 1024
TRUNCATION_SUFFIX = "…[truncated]"

FORBIDDEN_KEYWORDS = frozenset(
    {
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
)


def _cap_16k(value: str) -> str:
    size = len(value.encode("utf-8"))
    if size > TEXT_CAP_BYTES:
        raise ValueError(f"text is {size} bytes of UTF-8; the cap is {TEXT_CAP_BYTES}")
    return value


def truncate_utf8(value: str, cap: int) -> str:
    """Return ``value`` unchanged if it fits in ``cap`` UTF-8 bytes, else cut it and add the suffix.

    The result is at most ``cap`` bytes and always ends with ``TRUNCATION_SUFFIX``.
    """
    if len(value.encode("utf-8")) <= cap:
        return value
    budget = cap - len(TRUNCATION_SUFFIX.encode("utf-8"))
    # errors="ignore" drops a trailing partial sequence, so the cut lands on a character boundary.
    head = value.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return head + TRUNCATION_SUFFIX


def _truncate_excerpt(value: str) -> str:
    return truncate_utf8(value, EXCERPT_CAP_BYTES)


Text16K = Annotated[str, AfterValidator(_cap_16k)]
Excerpt8K = Annotated[str, AfterValidator(_truncate_excerpt)]


class _Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PredictedFile(_Artifact):
    path_glob: Text16K
    mode: Literal["read", "write"]


class PlanStep(_Artifact):
    description: Text16K
    files: list[Text16K]


class Plan(_Artifact):
    schema_version: Literal[1]
    summary: Text16K
    steps: list[PlanStep]
    predicted_files: list[PredictedFile]
    acceptance_criteria: list[Text16K]
    test_strategy: Text16K
    risk_level: Literal["low", "medium", "high"]
    out_of_scope_reason: Text16K | None


class PatchSummary(_Artifact):
    schema_version: Literal[1]
    summary: Text16K
    files_changed: list[Text16K]
    rationale: Text16K
    known_limitations: list[Text16K]


class TestReport(_Artifact):
    __test__ = False  # not a pytest test class

    schema_version: Literal[1]
    passed: int
    failed: int
    errors: int
    skipped: int
    failing_tests: list[Text16K]
    new_tests: list[Text16K]
    suite_command: list[Text16K]
    output_excerpt: Excerpt8K
    post_rebase: bool


class Finding(_Artifact):
    file: Text16K
    line: int | None
    severity: Literal["critical", "high", "medium", "low", "nit"]
    rationale: Text16K
    suggestion: Text16K | None


class ReviewReport(_Artifact):
    schema_version: Literal[1]
    verdict: Literal["approve", "request_changes"]
    findings: list[Finding]
    injection_suspected: bool


class HandoverNote(_Artifact):
    schema_version: Literal[1]
    goal: Text16K
    attempted: list[Text16K]
    current_state: Text16K
    evidence: list[Text16K]
    suggested_next_step: Text16K


class ConflictReport(_Artifact):
    schema_version: Literal[1]
    base_sha: Text16K
    onto_sha: Text16K
    conflicting_files: list[Text16K]
    resolution_attempted: bool


class ContextDigest(_Artifact):
    schema_version: Literal[1]
    goal: Text16K
    constraints: list[Text16K]
    open_questions: list[Text16K]
    artifact_ids: list[Text16K]


ARTIFACT_KINDS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        "plan": Plan,
        "patch_summary": PatchSummary,
        "test_report": TestReport,
        "review_report": ReviewReport,
        "handover_note": HandoverNote,
        "conflict_report": ConflictReport,
    }
)

ARTIFACT_MODELS: tuple[type[BaseModel], ...] = (*ARTIFACT_KINDS.values(), ContextDigest)

# Keys whose value maps arbitrary names to sub-schemas (the names are not keywords).
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
# Keys whose value is plain data, not a schema.
_DATA_KEYS = frozenset({"required", "enum", "const", "default", "examples"})


def _strictify(node: Any) -> Any:
    """Return a deep copy of schema ``node`` with every object made strict."""
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in FORBIDDEN_KEYWORDS:
            raise ValueError(f"unsupported JSON-schema keyword {key!r} in strict schema")
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        elif key in _DATA_KEYS:
            out[key] = deepcopy(value)
        else:
            out[key] = _strictify(value)
    props = out.get("properties")
    if out.get("type") == "object" or isinstance(props, dict):
        out["additionalProperties"] = False
        out["required"] = list(props) if isinstance(props, dict) else []
    return out


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value)
            else:
                found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def _assert_no_ref_cycle(schema: dict[str, Any]) -> None:
    defs: dict[str, Any] = schema.get("$defs", {})
    prefix = "#/$defs/"
    graph = {
        f"{prefix}{name}": {r for r in _refs(body) if r.startswith(prefix) or r == "#"}
        for name, body in defs.items()
    }
    root_refs = {r for r in _refs({k: v for k, v in schema.items() if k != "$defs"})}
    if "#" in root_refs:
        raise ValueError("recursive $ref cycle in schema (root references itself)")
    done: set[str] = set()
    active: list[str] = []

    def visit(ref: str) -> None:
        if ref in active:
            raise ValueError(f"recursive $ref cycle in schema: {' -> '.join([*active, ref])}")
        if ref in done or ref not in graph:
            return
        active.append(ref)
        for nxt in graph[ref]:
            visit(nxt)
        active.pop()
        done.add(ref)

    for ref in graph:
        visit(ref)


def strictify_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Strict form of any JSON schema (a new dict; ``schema`` is untouched).

    Raises ``ValueError`` on keywords the vendors' strict modes reject or on ``$ref`` cycles.
    """
    raw = dict(schema)
    _assert_no_ref_cycle(raw)
    result: dict[str, Any] = _strictify(raw)
    return result


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strict-mode JSON schema for ``model`` (pydantic's cached schema is untouched)."""
    return strictify_schema(model.model_json_schema())


def count_union_params(schema: Mapping[str, Any]) -> int:
    """Count property schemas with an ``anyOf`` or a list-valued ``type`` (``$defs`` included)."""

    def walk(node: Any) -> int:
        if isinstance(node, list):
            return sum(walk(item) for item in node)
        if not isinstance(node, dict):
            return 0
        total = 0
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                total += sum(
                    1
                    for sub in value.values()
                    if isinstance(sub, dict)
                    and ("anyOf" in sub or isinstance(sub.get("type"), list))
                )
            if key not in _DATA_KEYS:
                total += walk(value)
        return total

    return walk(schema)

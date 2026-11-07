import asyncio
import re
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

import anthropic
import pytest
from opentelemetry.trace import StatusCode

from quartet.domain.artifacts import Plan
from quartet.llm.base import (
    EMPTY_TOOLBOX,
    ArtifactInvalid,
    BudgetExceeded,
    ModelRefused,
    PricingUnavailable,
    ProviderError,
    RoleInput,
    RoleResult,
    Usage,
)
from quartet.llm.fake import FakeProvider, FakeTurn, ListRecorder, ScriptedToolbox
from quartet.llm.loop import LoopDeps, StopKind, run_step
from quartet.llm.pricing import PricingTable
from quartet.llm.prompting import TOOL_OUTPUT_CAP_BYTES
from quartet.llm.retry import RetryPolicy
from quartet.telemetry.redaction import Redactor
from tests.unit.llm._errors import status_error
from tests.unit.llm._records import cfg, limits, ok, plan_json, step

pytestmark = pytest.mark.anyio

TOOL_OUTPUT_OPEN = re.compile(r'^<untrusted_data_[0-9a-f]{16} kind="tool_output">')


async def _no_sleep(_seconds: float) -> None:
    return None


async def run_with(fake: FakeProvider, rec: ListRecorder, **limit_overrides: Any) -> RoleResult:
    deps = LoopDeps(
        PricingTable.load(), Redactor(), RetryPolicy(), time.monotonic, _no_sleep, lambda: 0.5
    )
    config = cfg(limits=limits(**limit_overrides))
    return await run_step(fake, config, RoleInput("p", (), "t"), EMPTY_TOOLBOX, rec, deps)


def read_a() -> FakeTurn:
    return FakeTurn(tool_calls=(("c1", "read_file", {"path": "a"}),))


async def test_tool_loop_then_artifact():
    tb = ScriptedToolbox({"read_file": ok("print(1)")})
    r, fake, rec = await step(
        [
            FakeTurn(tool_calls=(("c1", "read_file", {"path": "a.py"}),)),
            FakeTurn(final=plan_json()),
        ],
        toolbox=tb,
    )
    assert isinstance(r.artifact, Plan) and tb.calls == [("read_file", {"path": "a.py"})]
    assert TOOL_OUTPUT_OPEN.match(fake.tool_results[0][0].content)
    assert len(rec.llm) == 2 and r.totals.turns == 2 and r.totals.tool_calls == 1


async def test_adapter_begins_with_sorted_specs_and_prompt():
    tb = ScriptedToolbox({"b_tool": ok("x"), "a_tool": ok("x")})
    _, fake, _ = await step([FakeTurn(final=plan_json())], toolbox=tb)
    assert fake.began is not None
    assert [s.name for s in fake.began[2]] == ["a_tool", "b_tool"]
    assert "Plan it." in fake.began[1].user


async def test_duplicate_tool_names_rejected():
    tb = ScriptedToolbox({"a_tool": ok("x")})
    tb._specs.append(tb._specs[0])
    with pytest.raises(ValueError, match="duplicate"):
        await step([FakeTurn(final=plan_json())], toolbox=tb)


async def test_invalid_tool_input_not_executed(span_exporter):
    tb = ScriptedToolbox({"read_file": ok("x")})
    _, fake, rec = await step(
        [FakeTurn(tool_calls=(("c1", "read_file", {"path": 3}),)), FakeTurn(final=plan_json())],
        toolbox=tb,
    )
    (res,) = fake.tool_results[0]
    assert tb.calls == [] and res.is_error and "path" in res.content and rec.tools[0].is_error
    tool = next(s for s in span_exporter.get_finished_spans() if s.name == "tool.call")
    assert tool.status.status_code is StatusCode.ERROR


async def test_invalid_tool_input_message_has_no_values():
    tb = ScriptedToolbox({"read_file": ok("x")})
    _, fake, _ = await step(
        [
            FakeTurn(tool_calls=(("c1", "read_file", {"path": 3, "SECRET-KEY": "SECRET-VAL"}),)),
            FakeTurn(final=plan_json()),
        ],
        toolbox=tb,
    )
    content = fake.tool_results[0][0].content
    assert (
        "SECRET" not in content and "additionalProperties" in content and "$.path: type" in content
    )


async def test_unknown_tool_is_error(span_exporter):
    _, fake, _ = await step(
        [FakeTurn(tool_calls=(("c1", "rm_rf", {}),)), FakeTurn(final=plan_json())]
    )
    assert fake.tool_results[0][0].is_error and "unknown tool" in fake.tool_results[0][0].content
    tool = next(s for s in span_exporter.get_finished_spans() if s.name == "tool.call")
    assert tool.status.status_code is StatusCode.ERROR


async def test_parallel_calls_one_invalid():
    tb = ScriptedToolbox({"read_file": ok("x")})
    _, fake, _ = await step(
        [
            FakeTurn(tool_calls=(("c1", "read_file", {"path": "a"}), ("c2", "read_file", None))),
            FakeTurn(final=plan_json()),
        ],
        toolbox=tb,
    )
    assert [r.call_id for r in fake.tool_results[0]] == ["c1", "c2"]
    assert len(fake.tool_results) == 1
    assert [r.is_error for r in fake.tool_results[0]] == [False, True] and len(tb.calls) == 1


async def test_two_repairs_then_success():
    r, fake, _ = await step(
        [FakeTurn(final="{}"), FakeTurn(final="{}"), FakeTurn(final=plan_json())]
    )
    assert r.repairs == 2 and len(fake.user_texts) == 2
    assert fake.user_texts[0].startswith("Your previous reply was not a valid Plan object.")


async def test_third_invalid_raises_and_no_fourth_call():
    with pytest.raises(ArtifactInvalid) as e:
        await step([FakeTurn(final="{}"), FakeTurn(final="{}"), FakeTurn(final="{}")])
    assert e.value.attempts == 3 and len(e.value.calls) == 3
    assert e.value.totals is not None and e.value.totals.turns == 3


async def test_repair_message_never_echoes_model_text():
    bad_value = '{"summary": "SECRET-MARKER"}'
    bad_key = plan_json()[:-1] + ', "SECRET-KEY-NAME": 1}'
    _, fake, _ = await step(
        [FakeTurn(final=bad_value), FakeTurn(final=bad_key), FakeTurn(final=plan_json())]
    )
    assert "SECRET-MARKER" not in fake.user_texts[0]
    assert "SECRET-KEY-NAME" not in fake.user_texts[1]
    assert "extra_forbidden" in fake.user_texts[1]


async def test_unparseable_final_is_an_invalid_attempt():
    r, fake, _ = await step([FakeTurn(final=None), FakeTurn(final=plan_json())])
    assert r.repairs == 1 and "<root>: json_invalid" in fake.user_texts[0]


async def test_truncated_is_an_invalid_attempt():
    r, fake, _ = await step([FakeTurn(truncated=True), FakeTurn(final=plan_json())])
    assert r.repairs == 1 and "output token limit" in fake.user_texts[0]


async def test_refusal_raises_and_is_recorded():
    with pytest.raises(ModelRefused) as e:
        await step([FakeTurn(refused=True, refusal_category="cyber")])
    assert e.value.category == "cyber" and len(e.value.calls) == 1


async def test_tool_use_without_requests_is_a_provider_error():
    class Empty(FakeProvider):
        async def complete(self, *, timeout):
            turn = await super().complete(timeout=timeout)
            return replace(turn, stop=StopKind.TOOL_USE, tool_requests=())

    with pytest.raises(ProviderError) as e:
        await run_with(Empty([FakeTurn(final=plan_json())]), ListRecorder())
    assert e.value.retryable is False and len(e.value.calls) == 1


async def test_budget_breach_stops_before_next_call():
    with pytest.raises(BudgetExceeded) as e:
        await step(
            [read_a(), FakeTurn(final=plan_json())],
            toolbox=ScriptedToolbox({"read_file": ok("x")}),
            limits=limits(max_turns=1),
        )
    assert e.value.kind == "turns" and len(e.value.calls) == 1


async def test_usd_breach_after_expensive_call():
    big = Usage(0, 0, 0, 0, 1_000_000, 0)
    with pytest.raises(BudgetExceeded) as e:
        await step(
            [FakeTurn(final="{}", usage=big), FakeTurn(final=plan_json())],
            limits=limits(max_usd=Decimal("1"), max_total_tokens=10_000_000),
        )
    assert e.value.kind == "usd" and len(e.value.calls) == 1


async def test_cost_matches_pricing_table_to_the_cent():
    u = Usage(12_000, 100_000, 8_000, 0, 3_000, 1_000)
    r, _, rec = await step([FakeTurn(final=plan_json(), usage=u)])
    assert r.totals.cost_usd == rec.llm[0].cost_usd == Decimal("0.168000")


async def test_one_record_and_one_llm_span_per_call(span_exporter):
    await step(
        [read_a(), FakeTurn(final=plan_json())], toolbox=ScriptedToolbox({"read_file": ok("x")})
    )
    spans = span_exporter.get_finished_spans()
    role = next(s for s in spans if s.name == "role:planner")
    llm = [s for s in spans if s.name == "llm.call"]
    assert len(llm) == 2 and all(s.parent.span_id == role.context.span_id for s in llm)
    assert [s.name for s in spans].count("tool.call") == 1


async def test_spans_and_records_carry_no_content(span_exporter):
    marker = "MARKER-7f3a"
    await step(
        [
            FakeTurn(
                tool_calls=(
                    ("c1", "read_file", {"path": marker}),
                    ("c2", f"{marker}-tool", {}),
                )
            ),
            FakeTurn(final=plan_json(summary=marker)),
        ],
        toolbox=ScriptedToolbox({"read_file": ok(marker)}),
        task_text=marker,
    )
    for s in span_exporter.get_finished_spans():
        assert all(marker not in str(v) for v in s.attributes.values())
        assert s.events == () and marker not in (s.status.description or "")


async def test_cancellation_propagates_and_prior_calls_recorded():
    async def cancel(_args):
        raise asyncio.CancelledError

    rec = ListRecorder()
    with pytest.raises(asyncio.CancelledError):
        await step([read_a()], toolbox=ScriptedToolbox({"read_file": cancel}), recorder=rec)
    assert len(rec.llm) == 1


async def test_executor_exception_becomes_redacted_is_error(span_exporter):
    async def boom(_args):
        raise RuntimeError("token sk-ant-api03-" + "A" * 40)

    _, fake, _ = await step(
        [read_a(), FakeTurn(final=plan_json())], toolbox=ScriptedToolbox({"read_file": boom})
    )
    res = fake.tool_results[0][0]
    assert res.is_error and "RuntimeError" in res.content and "sk-ant-" not in res.content
    tool = next(s for s in span_exporter.get_finished_spans() if s.name == "tool.call")
    assert tool.status.description == "RuntimeError"


async def test_hung_tool_hits_wall_clock():
    async def hang(_args):
        await asyncio.sleep(10)

    with pytest.raises(BudgetExceeded) as e:
        await step(
            [read_a()],
            toolbox=ScriptedToolbox({"read_file": hang}),
            limits=limits(max_wall_seconds=0.2),
            real_clock=True,
        )
    assert e.value.kind == "wall_clock"


async def test_tool_call_cap_checked_before_execution():
    tb = ScriptedToolbox({"read_file": ok("x")})
    with pytest.raises(BudgetExceeded) as e:
        await step(
            [
                FakeTurn(
                    tool_calls=(
                        ("c1", "read_file", {"path": "a"}),
                        ("c2", "read_file", {"path": "b"}),
                    )
                )
            ],
            toolbox=tb,
            limits=limits(max_tool_calls=1),
        )
    assert e.value.kind == "tool_calls" and tb.calls == []


async def test_unpriced_served_model_recorded_at_ceiling_then_raises():
    rec = ListRecorder()
    with pytest.raises(PricingUnavailable):
        await step([FakeTurn(final=plan_json(), model="claude-mystery")], recorder=rec)
    assert rec.llm[0].cost_estimated and rec.llm[0].model == "claude-mystery"


async def test_provider_error_retried_and_attempts_recorded():
    rec = ListRecorder()
    await step(
        [FakeTurn(raises=status_error(anthropic.RateLimitError, 429)), FakeTurn(final=plan_json())],
        recorder=rec,
    )
    assert rec.llm[0].attempts == 2


async def test_keys_in_provider_errors_never_reach_spans(span_exporter):
    key = "sk-ant-api03-" + "A" * 40
    with pytest.raises(ProviderError) as e:
        await step(
            [
                FakeTurn(
                    raises=status_error(
                        anthropic.AuthenticationError, 401, message=f"bad key {key}"
                    )
                )
            ]
        )
    assert key not in str(e.value) and e.value.__cause__ is None
    for s in span_exporter.get_finished_spans():
        assert key not in (s.status.description or "") and s.events == ()
        assert all(key not in str(v) for v in s.attributes.values())


async def test_tool_raising_its_own_timeout_is_an_is_error_not_a_budget_breach():
    async def slow(_args):
        raise TimeoutError("test run timed out")

    _, fake, _ = await step(
        [read_a(), FakeTurn(final=plan_json())], toolbox=ScriptedToolbox({"read_file": slow})
    )
    assert fake.tool_results[0][0].is_error and "TimeoutError" in fake.tool_results[0][0].content


async def test_tool_output_capped():
    _, fake, _ = await step(
        [read_a(), FakeTurn(final=plan_json())],
        toolbox=ScriptedToolbox({"read_file": ok("x" * 100_000)}),
    )
    assert len(fake.tool_results[0][0].content.encode()) < TOOL_OUTPUT_CAP_BYTES + 100


async def test_tool_args_capped_before_recording():
    big = "y" * 100_000
    _, _, rec = await step(
        [FakeTurn(tool_calls=(("c1", "read_file", {"path": big}),)), FakeTurn(final=plan_json())],
        toolbox=ScriptedToolbox({"read_file": ok("x")}),
    )
    assert len(rec.tools[0].args_redacted["path"].encode()) <= 4_096 + 100


async def test_script_exhaustion_is_an_assertion():
    fake = FakeProvider([])
    with pytest.raises(AssertionError, match="script exhausted"):
        await fake.complete(timeout=1.0)


async def test_wall_clock_cut_off_call_writes_no_row():
    class Slow(FakeProvider):
        async def complete(self, *, timeout):
            await asyncio.sleep(10)
            return await super().complete(timeout=timeout)

    rec = ListRecorder()
    with pytest.raises(BudgetExceeded) as e:
        await run_with(Slow([FakeTurn(final=plan_json())]), rec, max_wall_seconds=0.1)
    assert e.value.kind == "wall_clock" and rec.llm == []


async def test_unknown_tool_name_and_bad_call_id_never_reach_telemetry(span_exporter):
    rec = ListRecorder()
    _, fake, _ = await step(
        [
            FakeTurn(tool_calls=(("MARKER call id with spaces", "MARKER-tool-9z", {}),)),
            FakeTurn(final=plan_json()),
        ],
        recorder=rec,
    )
    assert rec.tools[0].tool == "<unknown>" and rec.tools[0].call_id == "<invalid>"
    assert fake.tool_results[0][0].call_id == "MARKER call id with spaces"
    for s in span_exporter.get_finished_spans():
        assert all("MARKER" not in str(v) for v in s.attributes.values())


async def test_provider_error_with_billed_turn_is_recorded(span_exporter):
    class Billed(FakeProvider):
        async def complete(self, *, timeout):
            turn = await super().complete(timeout=timeout)
            raise ProviderError("bad turn", retryable=False, turn=turn)

    rec = ListRecorder()
    with pytest.raises(ProviderError) as e:
        await run_with(Billed([FakeTurn(final="{}")]), rec)
    assert len(rec.llm) == 1 and rec.llm[0].cost_usd > 0 and rec.llm[0].attempts == 1
    assert len(e.value.calls) == 1
    llm = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.call")
    assert llm.status.status_code is StatusCode.ERROR


class BilledFailures(FakeProvider):
    """Raises a retryable, billed ``ProviderError`` for the first ``failures`` calls."""

    def __init__(self, script: list[FakeTurn], failures: int) -> None:
        super().__init__(script)
        self._failures = failures

    async def complete(self, *, timeout):
        turn = await super().complete(timeout=timeout)
        if self.calls <= self._failures:
            billed = replace(turn, stop_reason="failed")
            raise ProviderError(
                "response failed: server_error", retryable=True, code="server_error", turn=billed
            )
        return turn


async def test_billed_retryable_failure_is_recorded_and_the_budget_gate_stops_the_retry(
    span_exporter,
):
    big = Usage(100_000, 0, 0, 0, 0, 0)  # about $0.50 on this model
    fake = BilledFailures([FakeTurn(usage=big)] * 3 + [FakeTurn(final=plan_json())], failures=3)
    rec = ListRecorder()
    with pytest.raises(BudgetExceeded) as e:
        await run_with(fake, rec, max_usd=Decimal("0.10"), max_total_tokens=1_000_000)
    assert e.value.kind == "usd"
    assert fake.calls == 1  # no further complete() after the breaching attempt
    (row,) = rec.llm
    assert row.stop_reason == "failed" and row.attempts == 1 and row.cost_usd > Decimal("0.10")
    assert e.value.totals is not None and e.value.totals.cost_usd == row.cost_usd
    assert len(e.value.calls) == 1
    llm = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.call")
    assert llm.status.status_code is StatusCode.ERROR and llm.attributes["error.type"] == (
        "BudgetExceeded"
    )


async def test_billed_retryable_failures_then_success_record_every_attempt():
    fake = BilledFailures([FakeTurn(), FakeTurn(), FakeTurn(final=plan_json())], failures=2)
    rec = ListRecorder()
    result = await run_with(fake, rec, max_turns=10)
    assert fake.calls == 3 and len(rec.llm) == 3
    assert [r.stop_reason for r in rec.llm] == ["failed", "failed", "end_turn"]
    assert [r.attempts for r in rec.llm] == [1, 1, 3]  # the last row: HTTP attempts of the call
    assert result.totals.turns == 3
    assert result.totals.cost_usd == sum((r.cost_usd for r in rec.llm), Decimal(0))
    assert result.calls == tuple(rec.llm)


async def test_billed_failure_that_exhausts_retries_is_recorded_once_per_attempt():
    fake = BilledFailures([FakeTurn()] * 4, failures=4)
    rec = ListRecorder()
    with pytest.raises(ProviderError) as e:
        await run_with(fake, rec, max_turns=10)
    assert len(rec.llm) == 4 and fake.calls == 4 and e.value.turn is None
    assert all(r.stop_reason == "failed" and r.attempts == 1 for r in rec.llm)


async def test_provider_error_span_description_holds_no_vendor_text(span_exporter):
    err = status_error(anthropic.BadRequestError, 400, message="echo: MARKER-body-text")
    with pytest.raises(ProviderError) as e:
        await step([FakeTurn(raises=err)])
    assert "MARKER" in str(e.value)  # the exception itself keeps the redacted text
    for s in span_exporter.get_finished_spans():
        assert "MARKER" not in str(s.status.description)
    llm = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.call")
    assert llm.status.description == "ProviderError (status 400, code None)"

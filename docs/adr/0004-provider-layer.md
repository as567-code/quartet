# ADR-0004: Provider layer

- Status: accepted
- Date: 2026-11-07

## Context

Quartet drives two vendors, Anthropic and OpenAI, through one role interface, `RoleRunner.run(...)` (PRD FR-15 and §9.4). Each role step must stay inside hard budgets, record every call, and never send an unvalidated tool input to a tool. The facts below were checked on 2026-11-06 against the SDK sources (anthropic 2.6.0, openai 3.46.0) and the vendor documentation. The Phase 2 live-verification gate re-verifies prices and model IDs before anything relies on them.

- **Both SDKs** use `httpx2` and retry by default (`max_retries=2`). Quartet needs its own retry policy, bounded by the step clock, so it turns SDK retries off.
- **The Anthropic beta tool runner** has no usage or latency hook and no per-tool wall-clock cap, and it keeps a server-held thread by default.
- **Only the beta Messages client** exposes `fallbacks`, `betas` and `usage.iterations`.
- **`transform_schema` and `output_format=Model`** (the `parse()` path) move `const` and `pattern` into descriptions, so the model is not held to them. Quartet sends its own strict schema and validates the output client-side.
- **The OpenAI `responses.stream()` helper** runs `json.loads` on strict-tool arguments while it iterates, so a malformed tool call raises inside the stream rather than becoming an answer to the model.
- **OpenAI `store`** defaults to true, which keeps responses for 30 days.
- **Anthropic streaming** is required when `max_tokens` is above about 21,333 and no explicit timeout is given.

## Decision

The decisions below are the plan's binding rulings P2-R1 to P2-R9, followed by the execution rulings made during the build that change behaviour or security posture. P2-R10 restates the Phase 0–1 carry-ins (R10 test packages, R38 empty env values, R39 README key wording). This ADR does not repeat them.

**P2-R1: a manual loop, not the beta tool runner.** Quartet uses its own vendor-neutral loop (`llm/loop.py`) for both vendors.
- *Reason:* the budget gate must sit in front of every call, and the step clock in front of every retry attempt. Each call needs its own latency, cost and span, and validation and redaction should be written once for both vendors.
- *Cost if wrong:* about 200 lines of loop to maintain.

**P2-R2: `eager_input_streaming` is not used.** Quartet never acts on partial tool input, because it reads the final message. Every tool input is validated client-side against its schema before a tool runs.
- *Cost if wrong:* a tool with large inputs starts slightly later.

**P2-R3: server-side fallback is a per-role flag with no default.** `RoleConfig.server_fallback` must be set explicitly. Smoke configurations may turn it on. Eval configurations keep it off, because another model answering would contaminate the M1–M3 measurements, and sticky routing (about 1 hour) carries the fallback model into later turns. `llm_calls` records both the requested and the served model.

**P2-R4: the orchestrator does not re-run a step after a validation or refusal failure.** The runner allows up to two repair turns (`MAX_ARTIFACT_REPAIRS = 2`), which is the "retried at most twice" of FR-15. After `ArtifactInvalid` or `ModelRefused`, the orchestrator moves the task to `NEEDS_HUMAN` without re-running the step (Phase 6).

**P2-R5: fallback cost is a conservative over-estimate.** Every `usage.iterations` entry is billed and priced at its own model's rate. The API bills declined attempts only for some refusal categories, so the recorded cost can exceed the invoice. The eval report must not claim invoice-exact cost for calls a fallback served.

**P2-R6: budget-gate semantics.**
- A call is refused when the step is already at a cap, or when the cap would be breached by the minimum the next call must cost. That minimum is the previous prompt re-sent at the cache-read rate, with no output.
- Output tokens are not reserved. Reserving `max_output_tokens` would refuse almost every first call.
- A step can exceed `max_usd` or `max_total_tokens` by at most one call: that call's input plus `max_output_tokens` of output.
- The gate runs before every HTTP attempt, retries included (FR-C1). A retryable failure whose response was billed (an OpenAI `failed` response with usage) is recorded first, so its usage counts before the next attempt is allowed; a breach raises `BudgetExceeded` and no further call is made.

**P2-R7: config files are package data.** `pricing.toml` and `models.toml` live in `src/quartet/llm/`. R21 forbids reading configuration from the working directory, and package data also works after installation. Prices are strings that become `Decimal`, and every price carries its source URL and retrieval date.

**P2-R8: OpenAI calls stream over the raw event stream.** `responses.create(stream=True)` is read event by event, and `store=False` is always sent. The helper is not used (see Context). Long `xhigh` reasoning would otherwise wait on one silent request. The wall-clock bound is the loop's `asyncio.timeout(remaining)` around the whole call, because httpx2 timeouts apply per read.

**P2-R9: always the beta Anthropic Messages client, streamed.** Only the beta client has `fallbacks`, `betas` and `usage.iterations`, and one client keeps one usage type in `turn_from_message`.

**P2-R10:** the Phase 0–1 carry-ins, cross-referenced above.

**Execution rulings** (made during the build; they supersede the plan where the two differ):

- **Log records are redacted when they are created** (P2-T3-R1). `install_log_redaction` replaces the log record factory, so every logger is covered. A traceback is rendered and redacted into `exc_text`, `exc_info` is cleared, and `stack_info` is redacted. The plan had deferred full log redaction to Phase 8, but FR-30 and the Phase 2 security check "keys in logs are scrubbed" are binding. *Cost if wrong:* handlers lose the live `exc_info` object, though the redacted text is kept.
- **Redaction patterns are tighter than the plan's** (P2-T3-R2, R5, R6, R7, R8). The keyword list adds `password` and `passwd`. Keyword matches use a left boundary, `(?<![A-Za-z0-9])`, so `ANTHROPIC_API_KEY=` matches. The `sk-` patterns carry a left guard, so identifiers such as `task-…` or `desk-…` are not redacted, but the guard still allows an escape or a percent-encoding before the key. The `Authorization` header is redacted to the end of its value. Quoted values are redacted through their closing quote, honouring backslash escapes. A known scheme word (Basic, Bearer, Token and so on) is skipped before a token. A parametrised test covers each keyword with each scheme. *Cost if wrong:* a few extra redactions in logs.
- **Known secrets are redacted in both forms** (P2-T3-R2). Each secret of eight characters or more is replaced exactly, and also in its `urllib.parse.quote(s, safe="")` form.
- **Redaction failures are caught narrowly** (P2-T3-R3). `str()` and formatting errors (`TypeError`, `ValueError`, `KeyError`, `IndexError`, `AttributeError`) are handled. Any other exception raised by a user's `__str__` propagates.
- **Third-party SDK debug logging is not configured by Quartet** (P2-T3-R4). Quartet sets no SDK log level. If someone enables DEBUG on the SDK loggers, request content can appear in logs. Logging configuration belongs to Phase 8.
- **Escaped-quote secrets are not fully covered** (P2-T3-R9, parked). A few escaped-quote shapes in unquoted values, and JSON-embedded secrets, can still leak through the regex layer. Exact known-secret replacement is the primary control, and structured redaction is Phase 8.
- **`StepBudget.limits` is read-only, and provider errors are raised outside the `except` block** (P2-T6-R1). The retry layer decides inside the block and raises outside it, so `__context__` never holds an unredacted SDK exception.
- **Untrusted data uses a per-step random delimiter** (P2-T7-R1, R2). `wrap_untrusted` produces `<untrusted_data_<nonce> kind="…">…</untrusted_data_<nonce>>`. The nonce is `secrets.token_hex(8)`, generated once per `run_step`, so the transcript is byte-stable within a step. Any `<` that could open or close an `untrusted_data` tag is also escaped. The system preamble names the tag pattern and is fixed, so it stays cacheable. Escaping every `<` was rejected because it corrupts source code shown to agents.
- **Telemetry identifiers are sanitised** (P2-T8-R1). A tool name appears in spans only if it is a declared tool, and otherwise as `<unknown>`. A call ID appears only if it matches `[A-Za-z0-9_.:-]{1,64}`, and otherwise as `<invalid>`. The adapter still answers the real call ID.
- **Unpriced models are not silently costed** (P2-R5 and the catalog check). A served model missing from `pricing.toml` is recorded at the provider's highest rate and flagged as estimated, and the step then stops with `PricingUnavailable`.
- **Smoke checks are capped in total** (`quartet llm smoke --max-usd`, default 1.00). Each check gets the remaining budget as its own cap, and checks after the cap are reported as skipped.
- **Final-review rulings.** FR-C1: a retryable billed failure is handed to the loop (`on_billed`) before the retry decision, gets its own row, and the gate (`before_retry`) runs before every further attempt. FR-I1: tool arguments are redacted (keys and values), then stripped of NUL and lone surrogates (`quartet.db.sanitize`), then capped; OpenAI arguments with `NaN` or `Infinity` count as invalid arguments; a `DBAPIError` from the ledger becomes `RecorderError` with the SQLSTATE only. FR-I2: the Anthropic adapter converts `httpx2.TransportError` raised while the stream is consumed into the SDK's connection or timeout error, so it is retried like any other. FR-M2: span descriptions and smoke output use `ProviderError.summary` (class, status, code), never the vendor text. FR-M4: `llm_calls.cost_estimated` records the ceiling-price flag.
- **Cassettes store full exchanges, scrubbed** (P2-T11-R1). Only the allowlisted response headers are kept, and request headers are never stored. `save_cassette` refuses to write if any decoded value still matches a redaction pattern. Recordings use synthetic prompts only, because credential scrubbing does not cover content and the repository is public.
- **The live cache test asserts a write or a read on the first call, and a read on the second** (P2-T13-R1). A cache hit is required on the second call, as the ROADMAP exit criterion says.

## Alternatives considered

- **The beta tool runner** (`client.beta.messages.tool_runner`) with `async_mcp_tool`. Rejected under P2-R1. It is beta, needs `thread=False` to avoid server-held state, has no usage or latency hook, and runs tools with no per-tool wall-clock cap.
- **`messages.parse()` (`output_format=Model`) and `transform_schema`.** Rejected. They rewrite `const` and `pattern` into descriptions, so the schema the model sees is weaker than the one Quartet validates.
- **The `responses.stream()` helper.** Rejected under P2-R8, because it crashes on malformed tool arguments instead of answering them.
- **Non-beta `messages.stream`.** Rejected under P2-R9, because it has no `fallbacks`, `betas` or `usage.iterations`.
- **OpenAI server-side state** (`store=true`, `previous_response_id`). Rejected. It keeps data for 30 days, and Quartet keeps the transcript itself.
- **A framework** (LangGraph, the OpenAI Agents SDK, the Claude Agent SDK) for the provider layer. Rejected in ADR-0001 for the orchestration core, and the same reasons apply here.

## Consequences

- The Phase 3 MCP client must convert MCP tool definitions into strict `ToolSpec`s that pass `ToolSpec`'s own check and fit `check_strict_budget` (at most 20 strict tools, 16 union-typed parameters per request, including the output schema).
- The orchestrator does not re-run a step after `ArtifactInvalid` or `ModelRefused` (P2-R4).
- Recorded cost is conservative for fallback-served calls (P2-R5). Eval reports must say so.
- A step can overshoot a cap by at most one call (P2-R6), including across retries.
- The ledger has one `llm_calls` row for each billed attempt with known usage, and a `Task.spent_usd` increment in the same transaction. A logical call that needed several billed attempts has one row per billed attempt: `stop_reason` `failed` and `attempts` 1 for each retried one, and the final row's `attempts` is the number of HTTP attempts. Calls whose usage is unknown (step-clock cut-off, mid-stream error or transport drop) are billed by the vendor but not recorded. The per-task cap is not enforced in this phase (Phase 6).
- Quartet owns the loop, the retry policy and the redaction. The vendor SDKs are kept behind two adapters, so an SDK upgrade touches only `anthropic_runner.py` and `openai_runner.py`. Both SDKs are pinned to a major version.
- Log redaction is a process-wide record factory. It is installed by `build_role_runner` and by every CLI command that loads settings, so it covers only records emitted after that point.
- No prompt, response, tool argument or tool output text reaches span attributes or exception messages (tested with an in-memory exporter). The Anthropic SDK's own spans are set to metadata-only. Quartet does not configure OpenAI SDK telemetry, and no test covers any spans that SDK emits.
- Nothing has run live yet. Cassette recording, `quartet llm verify-models` and `pytest -m live` are planned (the Phase 2 live-verification gate), and the catalog's `fallback_models` lists are empty until then.
- Relation to earlier decisions: ADR-0001 names "the Anthropic tool runner" for the per-role loop. This ADR supersedes that clause for the provider layer. ADR-0001 is left unedited as the record.

# Standalone tool-call schema qualification

Use this scenario to measure whether a model returns actual nested JSON arrays and
objects in a realistic `propose_plan` tool call. No Pi worker or Docker is
required. It checks structure, not whether the
proposed tasks are useful or safe to execute. Toolkit never executes those tasks
or their verification commands.

## Configure and review

After `mkdir -m 700 private`, write
`poundlock-toolkit example tool-schema --output private/tool-schema.json`. Replace the placeholder model
identity with the exact advertised model and record known backend/settings identity.
The `tool_schema.endpoints` object explicitly selects each boundary to test:

- `openai`: OpenAI-compatible base URL, including `/v1` where required.
- `ollama`: native Ollama server root; calls use `/api/chat`.

Keep only the supported/authorized entries. Configure both to measure them
separately against the same model; Toolkit does not guess equivalent endpoint
paths or silently fall back after an unsupported API response. Each configured
entry receives `trials` requests. `endpoint` and `api` remain the configuration's
primary endpoint identity for compatibility, but this scenario sends requests only
to `tool_schema.endpoints`. For other native runtimes, use their OpenAI-compatible
API; a general native adapter framework is outside this scenario.

The default has no credential. If direct authentication is needed, `credential_env`
is resolved on the execution machine and is sent to **every explicitly configured
boundary**. Use separate configs for different credentials. Endpoints must be
HTTP(S) without embedded credentials, query, or fragment. Redirects and environment
HTTP proxies are disabled. Declare authorized `local`/`private` data destination and
`paid: false`; unknown, external, and paid execution remains blocked.

```sh
poundlock-toolkit plan private/tool-schema.json --output private/schema-plan.json
poundlock-toolkit evaluate private/tool-schema.json --plan private/schema-plan.json --execute --output private/schema-run.json
poundlock-toolkit recommend private/schema-run.json --output private/schema-report.md
poundlock-toolkit export private/schema-run.json --output private/schema-evidence.json
poundlock-toolkit validate private/schema-evidence.json
```

Review the request count, total/request deadlines, response byte limit, and prompt
character estimate before `--execute`. The saved plan binds the full configuration,
fixture, and conversation. The character estimate is not a token count or a promise
that the conversation fits the model. Output tokens must leave input context.
There are no retries. Trials continue after ordinary schema/API failures and
alternate configured boundaries. Exhausted budgets and cancellation leave remaining
rows skipped; reusing an existing output path is refused.

## Smoke and longer qualification

`tool_schema.mode: "smoke"` uses a short synthetic request with the full nested
schema. The example requests three trials per configured boundary. Set
`mode: "qualification"` and an explicit larger `trials` count, such as 20, to use
a longer multi-turn synthetic reading-list project conversation. Review the new
plan and request/time budget. The supported range is 1–100 trials per boundary,
with an overall limit of 600 seconds; a large trial count can exceed that time
budget and leave skipped trials. Increasing the trial count does not increase the
time limit automatically.

Set `tool_schema.warmup_enabled: false` to skip the default unscored per-boundary
warm-up trial that runs before any scored trial (on by default, so first-load costs
such as Vulkan shader compilation never land inside a scored trial); it is recorded
separately under the result's `warmup` array and never counted in `summary`.

A slow-but-correct model can time out on every trial at the configured per-request
deadline (`limits.request_seconds`, up to 120s) or exhaust the overall budget
(`limits.total_seconds`, up to 600s) before finishing every planned trial: that model
still validly returned nothing, not that it validly returned a wrong answer. Reading
only `summary.passed` cannot distinguish the two. `plan` also warns ahead of
execution when a configured `requirements.min_decode_tokens_per_second` implies a
trial cannot finish within those caps at that speed.

Optionally provide `tool_schema.conversation`, a private array of role/content
messages (`system`, `user`, `assistant`). This replaces the synthetic conversation,
so include the instruction to call `propose_plan`. It stays in private execution
configuration, including the target's owned run directory until exact cleanup.
Default reports and exports contain its hash and source label, never the messages.
Keep real conversations and raw investigation evidence out of source control.
`demo` and `--readiness-only` are deliberately unsupported for this scenario; use
the existing separate readiness configuration for discovery-only checks.

The shipped schema models a realistic planning call with top-level
`acceptanceCriteria` and `verificationCommands` arrays and a `tasks` array. Each
task contains text fields plus `dependsOn`, `allowedPaths`, and
`verificationCommands` arrays. It is a stable synthetic stress fixture inspired by
planning workflows, not a versioned copy of a consumer's exact API. Changes to the
fixture or conversation change their recorded hashes.

## Local machine, direct URL, or SSH target

Direct `evaluate` executes from the current machine against configured URLs. For
local or SSH target execution, follow [TARGETS.md](TARGETS.md) to install the exact
same Toolkit package on the target and create the target configuration. URL
loopback addresses then refer to the target. Toolkit inventory describes the
execution target, not a different machine behind an inference URL.

```sh
poundlock-toolkit target-config private/target.json private/tool-schema.json --output private/schema-target-config.json
poundlock-toolkit target-plan private/target.json private/schema-target-config.json --output private/schema-target-plan.json
poundlock-toolkit target-run private/target.json private/schema-target-config.json --plan private/schema-target-plan.json --receipt private/schema-receipt.json --execute --output private/schema-run.json
poundlock-toolkit target-status private/target.json private/schema-receipt.json
poundlock-toolkit target-remove private/target.json private/schema-receipt.json --execute
```

`target-config` only binds observed inventory if **all** configured inference
endpoints are target loopback addresses. For a separate inference server, supply
authorized inventory/unknowns manually instead. SSH uses existing host trust and
agent configuration, with no credential forwarding. Existing target disconnect,
status, cancellation, checksum transfer, and exact-owned cleanup bounds apply to
this scenario; reconnecting does not start a second evaluation.

## Evidence contract and interpretation

Exports use `urn:poundlock-toolkit:tool-schema:1` and the packaged strict
`tool-schema-result.schema.json`. Existing qualification and legacy namespaces
remain unchanged. Each result records:

- Configuration and model/backend/settings identity hashes, fixture/conversation
  hashes, mode, conversation source, selected boundaries, and explicit limits.
- Every planned trial, timestamps/outcome, full-request latency, number of calls,
  expected-tool match, completion status, argument representation, outer parsing,
  schema validity, and overall `valid_call`.
- Sanitized field/type errors: for example, `$.tasks[0].dependsOn`, expected
  `array`, actual `string`, and diagnostic `nested_json: "valid_array"`.
- A `summary` per boundary containing `planned`, `attempted`, `passed`,
  `valid_call_rate`, `outer_parse_successes`, `schema_valid_calls`, and the same
  `passed` count again as `valid`, alongside `invalid` (attempted, not timed out,
  not a valid call), `timed_out` and `not_run` -- so a model that timed out on
  every trial and a model that answered wrong on every trial are distinguishable
  even though both show `passed: 0`.
- `predicted_per_second` per trial (and per warm-up row), when the boundary
  reports a decode speed: a collapse over a long qualification run is recorded as
  a possible VRAM spill, never asserted as the cause.
- A top-level `warmup` array: one unscored trial per boundary (absent when
  `warmup_enabled: false`), never counted in `summary`.

`valid_call` requires exactly one completed call of the expected function with
schema-valid arguments. JSON text arguments are parsed once; native object
arguments need no outer text parsing and count as parse success. A parsed scalar
can pass parsing while failing schema validation. Strings at array/object fields
remain failures even if they decode to the expected shape. `nested_json` only
reports parseability/type (`valid_array`, `valid_object`, `valid_string`, etc., or
`malformed`); it never repairs or replaces the value. Duplicate JSON keys,
non-finite numbers, malformed outer JSON, missing calls/arguments, and truncated
completion remain failures.

Unknown field names, actual string values, tool names, endpoint/model labels,
credentials, raw errors, and response bodies are not exported. Diagnostics expose
only fixture-known paths, array indices, and enumerated types/error codes, capped
at 64 with `errors_truncated` when more errors were encountered. They are shapes,
not text snippets.

The rate denominator includes every started trial, including HTTP/timeout,
missing-call, cancelled, and interrupted outcomes. Unstarted trials appear in
`planned` but not `attempted`; zero attempts yields a null rate. Thus a 100% rate
with skipped trials is visibly incomplete. Recovery preserves all rows and
recomputes the summary. Latency includes request and parsing time; it does not
invent prompt/decode timings or cold-cache classification. Compare rates only
alongside counts and matching configuration/fixture/conversation identities.

Ordinary CI uses deterministic fixtures and loopback servers, including a real
local target process. It does not establish live model, physical SSH, or consumer
acceptance. A bounded live run with a representative long conversation remains
necessary before relying on this evidence. Toolkit applies nothing: there is no
automatic admission or importer.

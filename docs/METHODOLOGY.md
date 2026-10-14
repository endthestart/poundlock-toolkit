# Evaluation methodology

One configuration freezes model ID/revision/artifact format/quantization, backend
name/build/commit, serving application release, template checksum, context/output/temperature/reasoning, prefix and
KV-cache settings, harness version/image/tools, inventory source and destination.
The serving application (for example ToshLLM wrapping llama.cpp) is recorded in
`backend.application` as `{name, version}` separately from the backend build and
commit, because several application releases can share one backend build. Set it to
`null` when the backend serves directly; leaving it out records it as unknown and
keeps identity incomplete. An identity fingerprint excludes endpoint and credential references. Configuration
fingerprints include private settings; hashes are not guarantees of anonymity.
`settings.kv_quantization` is recorded on the qualification document itself (schema
version 2), not only in the frozen configuration: a run measured under one KV cache
policy is otherwise indistinguishable from a run measured under another once the
config is gone. `settings.speculative` is optional and records `{type: "draft-mtp" |
"draft-model" | "none", draft_n_max}` when speculative decoding was configured;
absent means unrecorded, never inferred as `"none"`.

Stages are readiness → unscored warm-up → optional unscored prefix-cache probe →
basic API → tool compatibility when coding → streaming performance → task smoke →
promoted repetitions. The warm-up
is on by default (`limits.warmup_enabled`, disable to skip it) and sends one
request per run, before the first scored stage: not before each stage, trial or
repetition. `tool_schema` sends one per configured boundary, all before its first
scored trial (`tool_schema.warmup_enabled`). On llama.cpp Vulkan builds the
first request after a model load includes shader compilation; without a warm-up
that cost lands inside a scored trial and reads as a slow model. The warm-up's own
outcome is recorded stage `warmup` in `attempts`, with `independent_pass` and
`completion_pass` left null: it is evidence, never a gate, and never contributes to
a workload's pass/fail counts. Disabling or enabling it changes `limits`, one of
the reviewed configuration sections, so an approved plan drifts if a later run
turns it on or off without a fresh review. Direct extraction does not require
tool support or streaming to succeed. The separate streaming failure remains in
the report. Pi requires all four preliminary gates. A failed first task stops
promotion. Once promoted, every selected repetition runs within the deadline, even
after a later failure. There are no implicit retries or model substitutions.

`limits.prefix_cache_probe_enabled` (off unless set) adds four unscored requests
right after the warm-up, in this order: a synthetic prompt of at most about twelve
hundred tokens (and well inside the context window), opened by a per-run nonce so an
earlier run's cache cannot pre-warm it; the same prompt again (the exact repeat); an
agent turn, which is the prompt, the reply the server just returned, verbatim, and a
new short user message, the way an agent loop appends after the previous reply; and
the prompt with a short suffix inside the same user message (the edit inside the last
message). Each asks for at most eight tokens, and a reply cut off there is fine: only
how the prompt is processed is measured. They are stage `cache_probe`, with
`independent_pass` and `completion_pass` null, so like the warm-up they are evidence,
never a gate, and never touch a workload's counts. They are named in the plan and
counted in its request budget (four more requests), and turning the probe on or off
drifts the `limits` section. A failed request ends the probe; the rest are skipped, as
is the agent turn when the server returned no text to append after.

The evidence, strongest first: the server's own cached-token count for the request
(`usage.prompt_tokens_details.cached_tokens`, or llama.cpp `timings.cache_n`), kept
as `metrics.cached_tokens` and never inferred from speed; then the server's own
prompt-processing time (llama.cpp `timings.prompt_ms`, or Ollama's
`prompt_eval_duration`, read as `prompt_processing_seconds`); then time to first
token, which needs a streamed reply, so it exists for OpenAI-compatible servers only.
A request reused the prompt when at least half its prompt tokens came from cache or,
on timing alone, it was at least twice as fast as the first request. It did not when
under a tenth did or, on timing alone, it was at most 1.25 times as fast and the first
request took at least 0.25 s. In between, nothing is claimed. Timing is used only
after a warm-up that passed, because otherwise the first request can carry one-off
costs and a faster repeat would look like a hit; the client's time to first token
and the server's prompt time are never compared with each other; and a count that a
timing contradicts (a server that always reports zero) is `conflicting`, not a
verdict. Timing alone is the weaker evidence: even after a warm-up it can be
flattered by one-off costs that a small warm-up prompt does not absorb, so the
basis of each comparison is stored and shown with the verdict.
`prefix_cache_probe.observed` is `yes` only when the exact repeat and the agent turn
both reused the prompt, `no` only when neither did, and `inconclusive` otherwise, with
a `reason`. The numbers and thresholds behind it are stored beside it, and it can be
re-derived from the four attempts.

The edit inside the last message is judged the same way against the first request but
stored apart, as `edit_inside` and `edit_inside_verdict` (`reused`, `not_reused` or
`inconclusive`), and never changes `observed`. It diverges before the cached reply, so
the server has to roll back, which a hybrid or recurrent model cannot do cheaply: it
may restore an earlier checkpoint and reprocess, leaving a count that says reused
beside a request slower than cold (`conflicting`, so `inconclusive`). That is a real
cost when a harness edits the tail of a prompt, such as a pinned brief, but not a
missing cache. It runs last so the rollback cannot disturb the requests before it; if
it fails or times out it has no comparison and the verdict stands on the other three.

`settings.prefix_cache` stays what the operator declared: it is part of the identity
fingerprint and is never overwritten. The verdict is recorded beside it, the report
shows both, and where one value is needed the measured one is used. A measured `yes`
or `no` completes an `unknown` declaration, so `identity_complete` turns true when
nothing else is missing; an inconclusive result leaves `unknown` unknown and a
declared value standing. A declared `enabled` or `disabled` that the measurement
contradicts is a mismatch: the identity check fails, the report says so, and the
level stays `insufficient evidence` until the declaration is corrected or the
server's behaviour explained (some models cannot reuse a prefix even with the cache
on, and that is a finding about the setup, not a probe error). The measurement here
is `observed`, so it rests on the repeat and the agent turn alone: a server that
reuses on repeat and on append has a prefix cache whatever an edit to the tail costs
it, and a slow or unreused edit is never a mismatch. Two limits. The fingerprint
still covers only the declaration, so two runs that both declare `unknown` share one
whatever they measured; declare the setting when you know it. And one prompt, one
repeat, one agent turn and one edit say nothing about other prompts, longer contexts
or several slots or clients, while a `no` from counts is only as good as the server's
counters.

`memory_probe` (an optional config block naming exactly one of `pid` or `process`)
samples the named server process while the run executes. It reads, with no
privileges and no downloads: on Linux the process's resident set (`VmRSS`, from
`/proc/<pid>/status`) and, for each AMD card, the VRAM in use
(`/sys/class/drm/card*/device/mem_info_vram_used`); on macOS the process's resident
set from `ps`. A process name must match exactly one running process, and a PID is
checked, on every sample, to still be the process it was at the start. Samples are
taken at every attempt boundary and every half second between, so each attempt's
`metrics.peak_process_rss_bytes` is the highest value sampled during it and the
document's `memory_probe` block carries the first and highest over the run. That is a
lower bound on the true peak, never the peak itself. What it shows is narrow: RSS is
host memory the process has resident (weights held in GPU memory are not in it,
memory-mapped weights are, once touched); the VRAM counter covers every process on
the card, not only the server, and is stored per card as `device_wide`, never summed
and never attributed to the model; on Apple silicon CPU and GPU share one pool, RSS
does not establish the GPU-wired share, and nothing is added to system RAM; NVIDIA
and Intel GPUs, and AMD memory held in system-backed GTT, are not read, and an
empty `gpu_pools` means unknown, not zero. Sampling is evidence, never a gate: an unreadable sample leaves
the metric null, a server that exits mid-run is recorded as `process_exited`, and a
process that cannot be found or is named ambiguously is recorded as
`unavailable` with the reason. The probe reads processes on the machine Toolkit runs
on, so a non-loopback endpoint is a plan blocker (a remote server's memory cannot be
observed from its inference endpoint), and a synthetic run or `--readiness-only`
never samples. Under `target-run` the run happens on the target next to the server,
so there the PID is the target's. It is named in the plan, sends no requests, and
is covered by the reviewed inputs, so adding or changing it after review drifts
`memory_probe`.

Each attempt is saved before it starts, with UUID, UTC timestamp, prompt hash,
stage, repetition, outcome, failure code and nullable measurements. Completion
updates are atomic. Interruption, timeout, infrastructure/protocol failure, quality
failure, completion failure, skip and cleanup failure are visible. Reliability
denominators include all attempted tasks, including timeouts and interruptions;
skipped tasks are counted separately. Raw response/event bytes are transient and
discarded after checksum/verification. No free-form error body is retained.

The extraction fixture checks exact keys, values and JSON types for one synthetic
notice. A single surrounding Markdown code fence is removed before parsing and the
attempt is marked `fenced_json`: chat models commonly fence JSON, which is a
formatting convention rather than a protocol failure, and the deviation is recorded
rather than forgiven. Prose around the fence still fails. It does not qualify
general summarization, arbitrary documents or resistant parsing of adversarial
input. The coding fixture repairs one slug normalizer with
six examples plus a non-string rejection check. It accepts only a constrained Python
grammar, preventing obvious verifier monkeypatching and fabricated exit results.
It does not establish whole-application coding competence.

Pi gets a read-only fixture and a bounded writable tmpfs. The toolkit reads only
allowed regular output files, rejects links/additional files and verifies source
in a second no-network container. Worker self-report, observed exact verifier tool
invocation and normal harness completion are separate from independent acceptance.
Negative controls reject the broken starter, an incorrect function, unauthorized
files/links, false completion claims and a source-level process-exit bypass.

The investigation workload measures whether a model can locate one fact in a
read-only, multi-file corpus it cannot list directly: a generated `MANIFEST.md`
names every file, and the model must read as many of them as needed, then write a
self-contained verdict. Two frozen variants share the same corpus shape and
verifier: `findable`, where one entry satisfies the stated requirement, and
`absent`, where none does. A model that always answers "not satisfied" passes
every `absent` attempt and fails every `findable` one; `investigation_pair` refuses
to qualify unless both variants independently reach repeated task qualification
against the same `identity_sha256`, so that always-refuse strategy cannot pass by
averaging. The answer key lives only in the second, no-network verifier container,
never in the worker's mount, under any tool policy.

OpenAI first event may be reasoning or a tool; first useful output measures actual
content. A role-only event is not useful output. Missing output time stays null.
Unreported decode speed stays null; dividing output tokens by whole request or
post-first-event time is not called decode speed. llama.cpp's `timings` object is
read from both non-streamed responses and, on servers new enough to report it, the
final streamed chunk, giving `prefill_tokens_per_second`/`decode_tokens_per_second`
plus the raw `prompt_n`/`predicted_n` counts and, when speculative decoding (a
built-in draft head such as MTP, or a separate draft model) is active, `draft_n` and
`draft_n_accepted`. The report derives the acceptance rate (`draft_n_accepted` over
`draft_n`) from them for each attempt and shows it beside the counts; it is never
stored. It reads `unknown` when either count is missing, when no draft was offered
(0 of 0 is not a rate) or when the counts contradict each other. Only direct
requests report these counts: Pi runs record none. Native Ollama nanosecond fields
produce separate load, prompt-processing and decode durations/rates when valid. All
samples are recorded individually. The median successful task duration is a
descriptive convenience, alongside failures and sample counts; no significance or
confidence claim is made.

A model that spills out of VRAM commonly shows only as a decode-speed collapse, not
as a distinct failure code. An optional `requirements.min_decode_tokens_per_second`
adds a `throughput` check to `recommendation`: when set and the measured warm decode
speed (over passed task attempts, so the unscored warm-up never counts toward it)
falls below it, the check fails and the reasons name a possible VRAM spill as one
explanation, never as a confirmed cause -- the same measurement can also mean a
different backend, quantization or contended host. The floor applies only to
workloads whose attempts record a server-reported decode speed, which today means
direct extraction; there, a floor with no reported speed is `unknown`. Pi's event
stream (coding, investigation) carries per-request token counts but no server
timing, and Toolkit never derives a rate from wall-clock time, so those runs record
none. For them the floor is not applied: the check stays `pass`, the report carries
a note saying the floor was not applied, and the verdict is unchanged. A Pi run that
did record a speed would be held to the floor like any other. `schema_evaluation`
records `predicted_per_second` per tool-call trial for the same reason: a long
qualification run can reveal the same collapse partway through, even though that
scenario has no `recommendation`-style check of its own.

Task attempts have recorded a timeout as its own outcome since 0.1 (ADR 002 lists it
among the distinct failure outcomes): a coding run that timed out is `timeout`,
never `task_quality_failure`, and it stays in the reliability denominator. What
0.2.0 adds is for the `tool_schema` scenario, whose `summary` carried only `passed`,
so a model that timed out on every trial and one that answered wrong on every trial
both showed "0/5". `schema_evaluation.summaries` now splits `passed` into `valid`,
`invalid`, `timed_out` and `not_run`. 0.2.0 also adds a `warnings` list to `plan`:
when `requirements.min_decode_tokens_per_second` is set (stated, or fed back in from
a previous measurement), a configured `max_output_tokens` that would take longer
than the per-request or total time budget at that speed produces a warning before
execution, rather than a silent wall of timeouts after it.

Toolkit never establishes cold residency of an attached service, and each
attempt's `cache_state` stays `unknown`: a repeated request says nothing about any
other attempt. The one cache observation is the opt-in prefix-cache probe above,
which measures whether a prompt's prefix is reused, not whether a model was resident.
Configured cache policy differs from observed cache hits, and the report shows both.
Model-load time is unknown unless the native server reports it. Local RAM is not
remote RAM; Apple silicon system/unified values describe overlapping physical
storage and must not be added. Individual GPU pools are not summed into one
allocation. Peak memory is measured only for a named local process (`memory_probe`,
above); anywhere else it is unknown, and available memory/placement needs operator
evidence when unobservable.

Reports distinguish upstream compatibility, observed smoke pass, repeated fixture
qualification and insufficient evidence. Three verified repetitions permit the
narrow repeated level only with complete identity and satisfied explicit hard
constraints. Unknown privacy, required headroom or latency excludes a recommendation.
No cost ranking is attempted; no paid calls are supported. Model size labels are
not capability tiers. New configurations and materially changed settings need new
evidence. Timestamps are visible, and evidence older than 30 days is reported `stale`; the
operator still decides whether newer observations apply to the current machine.

Each workload's report lists named checks (`measured`, `freshness`, `identity`,
`serving_application`, `requirements`, `capacity`, `gates`, `latency`,
`throughput`, `compaction`, `repetitions`) with one status each: `pass`, `fail` (observed and not met),
`unknown` (not observed or not recorded) or `stale` (too old to rely on). The first
non-passing check is the `next_required_check`; the existing next useful test
remains alongside it. A report can also carry `Note:` lines, which never change the
level: today, only a decode-speed floor that does not apply to that workload.

In a long Pi coding session, once the conversation fills the model's context,
Pi's own compaction summarizes the history -- and a summary can silently drop an
instruction that was only ever stated once, such as the exact shape of the final
report. The default coding and investigation workloads still disable compaction
entirely, as before. `harness.compaction_gate` is a separate, opt-in coding-workload
variant: it enables compaction with a small forced context window and reserve, so a
compaction is expected to happen at least once during the fixed slug-repair task,
and counts completed compactions (`compaction_end` transcript events, leaving out
any that Pi reports as aborted or failed) into `metrics.compactions`. The fixture's
own report contract -- already independently verified, not merely self-reported --
is checked exactly as on every coding run; with the gate on, that check follows a
forced compaction. The compaction is not assumed: the qualification document records
the forced window and reserve as `compaction_gate`, and the `compaction` check
requires every passed coding attempt to have recorded at least one completed
compaction. A pass without one never exercised the gate, so the check is `unknown`,
the reason reads "the compaction gate did not trigger a compaction", and the level
is `insufficient evidence`, never a pass; the next useful test is a smaller forced
window or a larger reserve. The fix this models: the report contract is re-asserted
in the system role on every request by the fixture's Pi extension, rather than
relied on from the first user turn, because compaction summarizes conversation
history but never the system prompt. The same transcript also feeds
`metrics.loops_detected`: a simplified, fixture-scale port of periodic/low-variety
repeated-tool-call detection, counting each stuck stretch once rather than once per
call.

A pass/fail result from this toolkit proves the fixture's own narrow checks, not
that a change is mergeable. A blind rubric review, run once, found blocking defects
in every reviewed branch whose automated checks passed, and a model reviewing its
own prior output made a materially worse fraction of cases than it improved,
including one "fix" that deleted the service under review -- self-review is not a
substitute for an independent reviewer. The method: anonymize candidate diffs under
opaque labels, score every candidate against one fixed rubric with a `merge as-is` /
`merge after small fix` / `rewrite` verdict, and keep the label-to-identity key in a
separate file the reviewer never opens. `review-packet` exports exactly that: given
a private descriptor of `{identity, diff_path}` candidates, it writes an anonymised
`packet.md` and a fixed `RUBRIC.md` into the `--output` folder, which is what the
reviewer is handed, and the key to the file named by `--key-out`. `--key-out` is
required and must lie outside the packet folder: the command refuses a path inside
it, however spelled (`..` and symlinks included), and writes nothing, so handing
over the folder can never hand over the key. Labels are assigned in random order so
their sequence carries no information either. It reads pre-supplied diff files only
-- it never runs git or executes anything -- so it stays a pure offline export like
`export`/`recommend`. This is a limit on what this toolkit's evidence can claim, not
a workload it runs live; see [SUPPORT.md](SUPPORT.md) for exactly what was
exercised.

Both current schemas use Draft 2020-12 with closed objects. Schema version 2 added
`settings.kv_quantization`/`settings.speculative`, the `warmup` stage/workload,
the new decode-detail and compaction/loop metrics, the `compaction_gate` block, and
`requirements.min_decode_tokens_per_second`
-- all as optional fields or extended enums, so a schema version 1 document with
none of them still validates unchanged; only newly-created documents carry version
2. Release 0.2.1 adds more of the same, without a new schema version: the
`cache_probe` stage and workload, `metrics.cached_tokens`,
`limits.prefix_cache_probe_enabled`, and the optional `prefix_cache_probe` and
`memory_probe` blocks. A run that uses none of them writes what 0.2.0 wrote, and a
0.2.0 reader rejects a document that does. Release 0.2.2 changes the probe block the
same way: four requests, `agent_turn` in place of `extension`, and `edit_inside` with
`edit_inside_verdict`. A 0.2.1 block still validates, exports and reports as it was,
and a 0.2.1 reader rejects a 0.2.2 block. A plan reviewed with the older probe reads
as stale (`plan_version`) and needs review again. `normalize-legacy` validates against
the reviewed lab schema, preserves exact original bytes in a private directory and adds a checksum-only non-qualifying
envelope. It does not relabel historical "cold" or TTFT values, nor silently
rewrite original data.

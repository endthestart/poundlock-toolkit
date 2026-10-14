# Toolkit specification

CLI: `inspect`, `plan`, `evaluate`, `recommend`, `export`,
`validate`, `recover`, `normalize-legacy`, `review-packet`, offline `demo`,
`setup-plan`, `setup-script` and the `target-*` workflow. JSON configuration is
explicit and strict.
Plan output freezes a config fingerprint and bounded request/resource counts.
Evaluate requires the matching saved plan and `--execute`. Credentials use an
environment variable, never a config value. No automatic paid/external calls.

One selected model/endpoint per run; serial attempts, zero implicit retries,
at most five task repetitions, total deadline at most ten minutes. Operator may
combine result documents in recommend, but configuration fingerprints stay distinct.
Structured extraction uses one synthetic shipping notice. Coding repairs a Unicode
slug function using Pi, fixed tool surface and an independent protected verifier.
Smoke passes do not establish broad coding, review or summarization capability.

Two optional, opt-in measurements sit beside the fixtures, each named in the plan
and covered by its reviewed inputs. `limits.prefix_cache_probe_enabled` sends four
unscored requests after the warm-up (a prompt, the same prompt, an agent turn appended
after the reply, the prompt with a small edit inside its last message), counted in the
request budget, and stores a yes/no/inconclusive verdict from the repeat and the agent
turn beside the declared `settings.prefix_cache`; the measured value is the one
reported and a disagreement with the declaration is a mismatch that blocks
qualification. The edit inside the last message is reported apart and never changes
the verdict.
`memory_probe` names a local server process (PID or exact process name) and samples
its resident memory, and AMD VRAM per card on Linux, while the run executes: highest
sampled values only, never a peak claim, never summed, unknown for remote endpoints
and wherever no counter exists.

Qualification requires observed gates, independently passed task/completion checks,
cleanup success and sufficient repetitions, plus, where a compaction gate is
configured, a completed compaction in every passed coding attempt. Missing identity
or hard-constraint evidence produces insufficient evidence. Synthetic records never qualify hardware.
Reports expose failure counts and denominators, unknown metrics, age, constraints
and next useful test, without opaque aggregate scores.

Acceptance: clean install/offline use; both endpoint protocols tested without GPUs;
malformed/missing/wrong outputs rejected; timeouts, cancellation, promotion and
owned cleanup tested; negative verifier controls; strict schema/export validation;
standalone real inference and isolated Pi; local and SSH target use, with missing
live gates explicitly retained when infrastructure is unavailable. Linux and macOS are intended package platforms;
Windows and untested hardware remain unqualified.

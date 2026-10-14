# Poundlock Toolkit qualification report

Evidence applies only to the frozen fixture and configuration. No universal ranking or production admission is implied.

For extraction: no supplied configuration has sufficient evidence for repeated task qualification.

For coding: no supplied configuration has sufficient evidence for repeated task qualification.

## Run e12cba19-c655-4358-866e-652e7f77389c
Observed: 2026-09-13T15:15:35.909225+00:00. Configuration: `04bf83335896b413a0d6dd0eaa1b6e0edd239352b6291417bd4bebeea1b9e470`.
Backend: llama.cpp; API: openai; harness: direct.
Serving application release: not recorded by this evidence version.

### extraction: insufficient evidence
3/3 attempted task runs independently passed; 3 planned. Median successful task duration: 0.25 seconds.
Outcomes: pass=3

- Synthetic demonstration; not measured model or hardware qualification.
- Exact model/backend/template/cache identity is incomplete.
- Serving application release is not recorded; a backend build/commit alone cannot distinguish application releases.

Checks: measured=fail, freshness=pass, identity=unknown, serving_application=unknown, requirements=pass, capacity=pass, gates=pass, latency=pass, repetitions=pass.
Next required check: measured
Next useful test: Freeze missing identity/inventory, then plan a new bounded run.

### coding: insufficient evidence
0/0 attempted task runs independently passed; 0 planned. Median successful task duration: unknown seconds.
Outcomes: untested

- Synthetic demonstration; not measured model or hardware qualification.
- Exact model/backend/template/cache identity is incomplete.
- Serving application release is not recorded; a backend build/commit alone cannot distinguish application releases.
- A required readiness or compatibility gate did not pass.
- At least one attempt failed, was interrupted, or has no verified pass.
- Fewer than three verified repetitions; repeated qualification is not established.

Checks: measured=fail, freshness=pass, identity=unknown, serving_application=unknown, requirements=pass, capacity=pass, gates=unknown, latency=pass, repetitions=unknown.
Next required check: measured
Next useful test: Freeze missing identity/inventory, then plan a new bounded run.

| Stage | Workload | Repetition | Outcome | Seconds | First useful output | Decode tokens/s |
|---|---|---:|---|---:|---:|---:|
| readiness | readiness | 0 | pass (ok) | 0.0000 | unknown | unknown |
| api | api | 0 | pass (ok) | 0.2500 | unknown | unknown |
| performance | extraction | 0 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 1 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 2 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 3 | pass (ok) | 0.2500 | unknown | unknown |

Cache state is unknown. Attached service load/cold residency, remote placement, cost and unreported memory metrics remain unknown. No confidence intervals are claimed for these small samples.

## Run 8e6faeb5-a039-4d5c-939d-b84fe63f4090
Observed: 2026-09-13T15:15:35.923175+00:00. Configuration: `65e3b162ff5fce35400f6f044d1859af9754e7881f0a5bfc8e314dea2e112a8a`.
Backend: ollama; API: ollama; harness: direct.
Serving application release: not recorded by this evidence version.

### extraction: insufficient evidence
3/3 attempted task runs independently passed; 3 planned. Median successful task duration: 0.25 seconds.
Outcomes: pass=3

- Synthetic demonstration; not measured model or hardware qualification.
- Exact model/backend/template/cache identity is incomplete.
- Serving application release is not recorded; a backend build/commit alone cannot distinguish application releases.

Checks: measured=fail, freshness=pass, identity=unknown, serving_application=unknown, requirements=pass, capacity=pass, gates=pass, latency=pass, repetitions=pass.
Next required check: measured
Next useful test: Freeze missing identity/inventory, then plan a new bounded run.

### coding: insufficient evidence
0/0 attempted task runs independently passed; 0 planned. Median successful task duration: unknown seconds.
Outcomes: untested

- Synthetic demonstration; not measured model or hardware qualification.
- Exact model/backend/template/cache identity is incomplete.
- Serving application release is not recorded; a backend build/commit alone cannot distinguish application releases.
- A required readiness or compatibility gate did not pass.
- At least one attempt failed, was interrupted, or has no verified pass.
- Fewer than three verified repetitions; repeated qualification is not established.

Checks: measured=fail, freshness=pass, identity=unknown, serving_application=unknown, requirements=pass, capacity=pass, gates=unknown, latency=pass, repetitions=unknown.
Next required check: measured
Next useful test: Freeze missing identity/inventory, then plan a new bounded run.

| Stage | Workload | Repetition | Outcome | Seconds | First useful output | Decode tokens/s |
|---|---|---:|---|---:|---:|---:|
| readiness | readiness | 0 | pass (ok) | 0.0000 | unknown | unknown |
| api | api | 0 | pass (ok) | 0.2500 | unknown | unknown |
| performance | extraction | 0 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 1 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 2 | pass (ok) | 0.2500 | unknown | unknown |
| task | extraction | 3 | pass (ok) | 0.2500 | unknown | unknown |

Cache state is unknown. Attached service load/cold residency, remote placement, cost and unreported memory metrics remain unknown. No confidence intervals are claimed for these small samples.

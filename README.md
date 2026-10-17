# Poundlock Toolkit

Find out which models, runtimes and settings actually work on hardware you control.
Select a local machine or an SSH target, inspect its hardware, review setup and a
bounded test plan, then compare the configurations actually tested. Toolkit is a
finite CLI, with no daemon, production scheduler or model manager.

Python 3.11+ on Linux and macOS; Apache-2.0 licensed. Windows is untested.
Offline tests cover transport and scoring; they never establish hardware or model
quality, which takes a live run. Changes are in [CHANGELOG.md](CHANGELOG.md).

## Start offline

Install into a virtual environment from PyPI (or `pip install .` from a checkout).
Installation downloads the schema-validator dependency from your configured package
index. These commands only inspect the local machine; they do not call a model or a
paid API.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install poundlock-toolkit
.venv/bin/poundlock-toolkit inspect
.venv/bin/poundlock-toolkit --help
```

With the environment activated, try a synthetic end-to-end report. It needs no model,
Docker or credentials, and works from any folder:

```sh
poundlock-toolkit example openai --output openai.json
poundlock-toolkit plan openai.json --output plan.json
poundlock-toolkit demo openai.json --output demo.json
poundlock-toolkit recommend demo.json --output report.md
poundlock-toolkit export demo.json --output qualification.json
poundlock-toolkit validate qualification.json
```

## Prepare and test a machine

1. Follow [local/SSH target selection and installation](docs/TARGETS.md).
2. Read the [hardware/backend guidance](docs/SUPPORT.md); keep unknowns explicit.
3. Follow the [isolated standalone CPU/Pi recipe](docs/STANDALONE.md), or attach to
   an existing authorized endpoint. Review every setup command and resource bound.
4. Review a saved plan, execute explicitly, retain the receipt and inspect failures.
5. Compare reports, then keep the private evidence with the configuration you deploy.

To decide what to run and how to read the result, start with
[what to test and how to score it](docs/METHOD.md).

Examples contain placeholder model identities and image digests. Replace them with
verified artifacts before execution. No model weights, drivers or infrastructure
services are installed automatically. The standalone recipe downloads zero bytes;
its explicit prerequisite preparation has separate download and build costs.

## Tool-call schema adherence

[Measure nested tool-call JSON Schema adherence](docs/TOOL_SCHEMA.md) directly,
locally or over SSH, without Pi or Docker. Repeated smoke or longer conversation
trials score native Ollama and OpenAI-compatible calls separately, retain invalid
nested strings as failures, and export content-free field/type diagnostics.

## Existing endpoint

Write an example into an ignored private folder and edit it:
`poundlock-toolkit example openai --output private/endpoint.json` (or `ollama`;
`poundlock-toolkit example` lists them all).
Set the endpoint, advertised model ID, revision, quantization, backend build
(`backend.version`, optional `backend.commit`), serving application release
(`backend.application`: `{name, version}`, or `null` when served directly), template checksum, context, reasoning and inventory. Preserve unknowns as `null`.
OpenAI bases include `/v1`; native Ollama bases use the server root. With
`reasoning: "off"` the OpenAI boundary omits `reasoning_effort`, which lets thinking
models such as Qwen 3.5 on Ollama think anyway; set `settings.reasoning_off_effort`
to `"none"` to send it explicitly (direct requests and Pi). Destination and
cost declarations express operator knowledge and authority, independently of URLs.

```sh
poundlock-toolkit plan private/endpoint.json --output private/plan.json
poundlock-toolkit evaluate private/endpoint.json --plan private/plan.json --execute --output private/run.json
poundlock-toolkit recommend private/run.json --output private/report.md
poundlock-toolkit export private/run.json --output private/qualification.json
```

Two measurements are opt-in, and each is part of the reviewed plan. Set
`limits.prefix_cache_probe_enabled` to send four extra unscored requests that
measure the prefix cache instead of taking `settings.prefix_cache` on trust; the
report shows the measured and declared values side by side and flags a mismatch, and
says separately what an edit inside the last message cost. Add
`"memory_probe": {"pid": 1234}` (or `{"process": "llama-server"}`, which must match
exactly one process) to sample that local server process's memory during the run:
highest sampled values only, for a loopback endpoint only, and unknown everywhere
else. See [methodology](docs/METHODOLOGY.md) for exactly what each does and does not
show.

`credential_env` names a secret-manager-supplied variable for direct checks; never
put the secret in a file or argument. Pi requires the isolated unauthenticated
boundary in the standalone recipe and refuses credential-protected endpoints.
The complete configuration must match the saved plan; otherwise evaluate exits 4
and names the drifted configuration sections. Without `--execute`, a
pending-consent result exits 3. Add `--readiness-only` to both plan and evaluate for
one model-list request with no inference. A readiness pass is not qualification.

`model.id` must be a name the server lists (`GET /v1/models` rows, or Ollama's
`/api/tags`); otherwise readiness fails with `model_not_listed`. Toolkit never
substitutes a name.

### Exit codes

`poundlock-toolkit --help` lists the public CLI codes:

| Code | Meaning |
| --- | --- |
| 0 | Command or selected attempts succeeded |
| 1 | An evaluation contains a failure or skip |
| 2 | Invalid input, unmet prerequisites, or a plan with blockers |
| 3 | Pending consent (`evaluate` without `--execute`) |
| 4 | A reviewed assumption drifted or an attach-time check failed; the JSON output names the check, what drifted and the next required step |
| 130 | Cancelled |

`recommend` accepts several results, retaining their separate identities, repetitions,
failures and unknowns. Reports qualify only the narrow fixtures actually tested.
Public exports omit arbitrary endpoint/model labels; retain private configurations
and mappings for reproducibility. Review even allowlisted exports before disclosure.

`recommend --format json` writes the same verdicts as data for other programs: levels,
pass and attempt counts, investigation pairs and tool-schema counts, in the versioned
shape `urn:poundlock-toolkit:recommendation:1`
([schema](src/poundlock_toolkit/data/recommendation.schema.json)). Version 1 only gains
optional fields. The supported interfaces are the CLI, this summary and the result
schemas; Toolkit's Python modules can change between releases.

## Reference

- [Specification](docs/SPEC.md), [architecture decisions](docs/adr/001-product-boundaries.md)
- [Method: what to test and how to score it](docs/METHOD.md)
- [Methodology and result semantics](docs/METHODOLOGY.md)
- [Field notes from long-run operator use](docs/FIELD-NOTES.md)
- [Recovery, retention and removal](docs/OPERATIONS.md)
- [Source and dependency notices](THIRD_PARTY.md)
- [Contributing](CONTRIBUTING.md), [security](SECURITY.md)

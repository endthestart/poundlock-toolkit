# Changelog

## 0.5.0 (unreleased)

- `poundlock-toolkit example NAME --output FILE` writes a packaged example
  configuration. The README quickstart now works from a wheel install, with no
  checkout; `poundlock-toolkit example` lists the names.
- The top-level `examples/` folder is gone from the checkout and the sdist. Its
  configurations moved into the package, so replace paths such as
  `examples/openai.json` with `poundlock-toolkit example`. The 0.1.0 example
  results and their report were dropped.
- `recommend --format json` writes the report's verdicts as data, in the versioned
  shape `urn:poundlock-toolkit:recommendation:1`. Other programs, such as Poundlock's
  capability converter, read this instead of importing Toolkit modules. It records
  what each run sent about thinking (`reasoning_sent`), since `reasoning: off`
  omits the field on the OpenAI boundary but disables thinking on native Ollama.
- The result schema drops the `verifier_failed` and `identity_mismatch` failure
  codes: nothing writes them, and no recorded result uses them.
- CI tests Python 3.11, 3.12, 3.13 and 3.14, and attaches a CycloneDX SBOM of the
  wheel install to each release.
- A release tag runs the full CI workflow on the tagged commit and publishes exactly
  the wheel and sdist it tested.

## 0.4.0 (October 2026)

The first public release.

- Licensed under Apache-2.0 (previously MIT).
- Independent of Poundlock: `publish-capabilities`, its vendored schema and the
  Poundlock model-name explanation are gone. A model the server does not list fails
  readiness as `model_not_listed`, and the `model_name_mismatch` code is retired.
- CI runs on GitHub Actions.

## 0.3.1 (October 2026)

- Pi 1.1.0 in the Pi recipe and examples; setup accepts it.
- The Pi recipe removes npm, npx and corepack after installing Pi, and takes Debian's
  security updates. CI now builds the recipe and fails on a fixable High or Critical
  finding.
- CI scanners: Trivy 0.75.0, osv-scanner 2.7.0, Semgrep 1.180.0; refreshed base images.

## 0.3.0 (October 2026)

- Pi 1.0.3. The Pi recipe installs Pi and its dependencies with `npm ci` from
  `recipes/pi/package-lock.json` and checks the CLI version; setup accepts 1.0.3.
- Toolkit qualifies model servers directly, by design. The Pi workload sends no
  credential yet, so a key-protected server is tested with its key off or on loopback.

## 0.2.3 (5 October 2026)

Pi 1.0.0. CI installs its tools from a hashed lock and runs its container with no
capabilities.

## 0.2.2

The agent-turn prefix-cache probe.

## 0.2.1

`docs/METHOD.md` (what to test and how to score it); peak memory of a named local
server process; the opt-in three-request prefix-cache probe.

## 0.2.0

Warm-up, spill and timeout evidence; KV-cache and speculative-decoding settings; the
compaction-gate rule.

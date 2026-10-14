# Contributing

Install the package in a virtual environment, then run
`python -m unittest discover -s tests -v`. Use `pip install -e .` for a development
checkout (repeat it after moving the checkout). Some tests run `python -I`, which
ignores `PYTHONPATH`. The test suite uses synthetic data and local fixture servers.
It needs no GPU, model weights, or paid credentials. Live tests are separate and
require a reviewed plan and `--execute`.

Keep evaluation logic separate from presentation and adapters. Version schemas
when their meaning changes. Preserve failed attempts, unknown measurements, and
the exact setup tested. Add negative controls for new fixtures. Do not add model
catalogs, silent downloads, service managers, or automatic escalation.

Contributions are accepted under the Apache License 2.0, as section 5 of the
license describes. Keep third-party notices intact.

Never put real endpoints, private addresses, personal paths, credentials, or raw
model output in a fixture, test, or issue. Use synthetic values and the reserved
example domains and address ranges.

CI runs secret, lockfile, and static-analysis scans, then lint, formatting, the
offline tests, the Pi policy controls, and clean wheel and sdist installs. Locally:
`ruff check src tests scripts`, `ruff format --check src tests scripts`,
`node tests/test_policy.mjs`, then `python -m build` and
`python scripts/check_artifacts.py` in a dedicated environment.

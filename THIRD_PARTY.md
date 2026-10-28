# Source, fixture and dependency inventory

Toolkit-owned code, documentation, and fixtures use the project's Apache-2.0
license. The package contains no third-party model outputs or weights.

| Material | Origin and treatment | License/redistribution status |
|---|---|---|
| Probe/timing/hash/gate primitives | Adapted from the author's earlier research; no raw logs or config copied | Toolkit Apache-2.0; provenance in docs/SOURCE.md |
| Seven slug behavioral cases | Adapted from the author's slug-normalization fixture | Toolkit Apache-2.0; starter is intentionally incorrect |
| Shipping-notice and investigation fixtures | Newly authored synthetic data | Toolkit Apache-2.0 |
| Legacy result schema | Earlier schema retained to validate older result files | Toolkit Apache-2.0; no historical results bundled |
| JSON Schema validator | `jsonschema` >=4.23,<5; runtime validation | MIT; installed dependency, not vendored |
| `attrs`, `referencing`, `jsonschema-specifications`, `rpds-py`, `typing-extensions` | Transitive resolved validator dependencies; exact versions retained with release artifact evidence | Upstream distribution licenses retained by package installer; not vendored |
| setuptools | Build dependency >=84.1 | MIT; not vendored |
| Python | External runtime >=3.11 | Python Software Foundation license; not bundled |
| Pi | External operator-built image; `recipes/pi` pins 1.1.0 and its dependencies by lockfile | Upstream MIT; installed from npm at image build, no Pi code copied |
| Docker | External operator prerequisite | Distribution-specific Docker terms; no engine/image redistribution |
| llama.cpp, Ollama, oMLX | Referenced upstream serving choices | Not redistributed; operator checks exact software/model artifact licenses |

Dependency metadata can be inspected with `python -m pip show jsonschema` and
the installed distributions' license files. The package declares direct bounds;
production packaging should lock the chosen environment. No fixture license or
model redistribution permission is inferred merely from public availability.

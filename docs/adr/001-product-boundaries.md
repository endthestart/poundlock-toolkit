# ADR 001: A standalone evaluation library and CLI

Status: accepted for local first release, 2026-09-13.

Toolkit inspects, plans, evaluates, recommends, and exports results. It does not
manage production model runtimes or application workflows. Hardware capacity alone
does not show that a model can do a task reliably.

Use Python modules with one endpoint interface and one Pi adapter. No daemon, GUI,
plugin registry, controller or production scheduler. Finite SSH transport runs
the same evaluator; separately reviewed setup scripts create and remove only owned
temporary infrastructure. No server database access, automatic downloads or profile
application. Backend (llama.cpp/Ollama/oMLX) and harness (direct/Pi) are independent.

Export is an offline review document. No qualification-import API is assumed.
An endpoint does not reveal remote GPU placement or inference data destination.

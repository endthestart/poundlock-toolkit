# ADR 003: Owned disposable workers and Apache-2.0

Status: accepted for local first release, 2026-09-13.

Pi code execution requires a local Unix-socket Docker engine with verified resource
limit support, an existing digest-pinned Docker image, an explicitly
selected existing internal Docker network and an inference endpoint accessible on
that network. Toolkit never starts Docker or creates fleet infrastructure. Worker
root filesystem is read-only; writable work/tmp/config storage is bounded tmpfs.
Non-root execution, no capabilities, no Docker socket, no host writable mounts,
bounded CPU/RAM/processes and wall time are enforced. Only the configured internal
network is reachable. That network is operator-authorized trust scope, not an
endpoint-specific firewall. The separate verifier has no network. Fixtures are
mounted read-only, checked by digest and verified independently of worker reports.

Only exact random container names owned by the run can be removed. Attached model
servers are never stopped/unloaded. Cancellation retains attempt evidence and
records cleanup failure. No unsafe host execution fallback when Docker is missing.

Toolkit-owned code, docs and adapted fixtures use Apache-2.0 (MIT before the
first public release). Dependencies retain their own licenses. Ownership and
dependency inventory remain in THIRD_PARTY.md.

For the fixed slug task, a read-only Pi extension restricts file tools to the two
task files (plus verifier read access), and bash to the exact verifier command.
Arbitrary shell execution is outside this fixture; Docker remains mandatory.

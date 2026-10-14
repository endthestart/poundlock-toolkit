# ADR 002: Attempts and unknowns are first-class evidence

Status: accepted for local first release, 2026-09-13.

Persist each attempt before execution and after completion. A surviving running
record from an abrupt host loss is recoverable as interrupted, never passed.
Use readiness, API/tool, short probe, then promoted repetition gates. No retries
inside an attempt. Infrastructure/protocol/quality/completion/timeout/cancellation
and cleanup failures are distinct. Repetitions remain individual observations.

Measure first useful content separately from first reasoning or tool event. Only
server-reported decode and prefill timing produce those rates. An attached request
cannot establish cold residency, so cache state remains unknown. RAM, unified
memory and each GPU pool remain separate; null is unknown and [] means no observed
pools, not proof of CPU-only hardware.

Private configuration freezes model revision/format/quantization, backend version,
settings, template identity, context, reasoning and tools. Public evidence contains
fingerprints of free-form identities and strict numeric/enum fields, never endpoint,
raw response, private labels, prompt text, errors or credentials. Raw response
retention is disabled; transient bytes are discarded. Hashes are provenance, not
anonymity guarantees. Review exports before disclosure.

Legacy normalization preserves exact original bytes privately and creates a new
non-qualifying envelope with a checksum. Historical metric semantics are not fixed
by relabeling them. Toolkit v1 and lab v1 have different schema namespaces.

# Recovery, retention and removal

Ctrl-C or SIGTERM interrupts the current attempt, saves cancellation and skips
remaining work. Request and total deadlines are enforced on Linux/macOS. Owned
container cleanup can add up to ten seconds per container (at most two) after a
deadline. No shared model/service is unloaded or stopped. Export does not mutate
the source document. CLI exit codes are listed in the [README](../README.md#exit-codes).
A cancelled attempt is recorded and the evaluation exits 1; 130 means cancellation
outside an active recorded attempt.

On hard host/process loss, preserve the result and mark running entries interrupted:

```sh
poundlock-toolkit recover private/run.json --output private/recovered.json
```

This is evidence recovery, not job resumption. A new run has a new output path and
identity; no failed attempts disappear. Record external interventions in operator
notes alongside the private config; do not merge them into public free-form fields.
The original document remains unchanged. Do not blindly replay a still-running job.

Owned Pi container names are recorded before creation in private `*.owned.json`
sidecars. Normal cleanup removes exactly those names. After SIGKILL, the worker's
internal watchdog terminates its container within the configured task deadline
plus 20 seconds. A stopped container may remain. Inspect each name from that
specific sidecar, then explicitly remove only that resource with `docker rm -f`.
Do not use global Docker prune, name-pattern cleanup, service shutdown or model
unload. If cleanup failed, the result remains an infrastructure failure.

Readiness HTTP 401/403 needs an authorized credential for direct evaluation; 429
or 5xx requires capacity/recovery, not a larger model. Missing model IDs
(`model_not_listed`) require review of the exact advertised alias. No downloads are
attempted. Invalid JSON,
missing finish markers and wrong tool arguments remain protocol/completion failures.
Missing Docker or image/network isolation leaves coding unqualified. Fix the
prerequisite outside Toolkit within operator authority, then make a new bounded plan.

Retention is explicit: configurations, result JSON, ownership sidecars and reports
remain until the operator deletes them. Raw HTTP bodies, Pi event transcripts and
generated code are **not retained**. Raw HTTP/event bytes exist transiently in memory or owned container tmpfs.
The allowlisted candidate source is briefly copied to a host fixture directory
inside a private 0700 temporary parent for independent verification; normal cleanup
removes it. A host crash can leave that exact temporary directory for manual retention
review. No raw-log opt-in exists. Debugging therefore requires
a new bounded run and separately reviewed logging; Toolkit never exports raw logs.
Private files are written 0600, directories 0700 where created. Protect parent
directories and avoid shared output paths. Hashes can reveal equality and should
still receive disclosure review.

Removal: settle any active owned worker, inspect its sidecar, remove remaining owned
containers if needed, then uninstall `poundlock-toolkit` from its virtual environment
or remove that dedicated virtual environment. Delete selected retained private
results/configs separately according to your policy. Toolkit has no daemon, system
service, global config, background task or production registration to remove.
Do not delete unrelated model weights, user repositories or other services' resources.

For target receipts, SSH disconnects and exact remote removal, see [TARGETS](TARGETS.md).
For finite setup server/network ownership, see [STANDALONE](STANDALONE.md).

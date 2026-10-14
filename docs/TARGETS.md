# Local and SSH targets

Both modes launch the same installed Python package on the selected machine.
Client and target must have identical package file fingerprints; a mismatch stops
before target writes. This compares package code/data, not interpreter or dependency
bytes. Install the same tested wheel and record the resolved environment on both.
Hardware, backend paths, endpoint URLs and environment variables in an evaluation
belong to the **target**, not the client. Target inventory does not establish the
hardware behind a nonlocal endpoint.

## Install and select

Use a dedicated Python 3.11+ environment on each machine. Transfer the release
wheel and SHA256SUMS through your existing trusted SSH account; verify the wheel's
SHA-256 against the release checksum before installing. For offline installation,
prepare a dependency wheelhouse for the target OS/architecture and use pip's
`--no-index --find-links /absolute/path/to/wheelhouse`. Compiled dependency wheels
are platform-specific. Do not copy the client's virtual environment to another OS.

```sh
python3 -m venv "$HOME/toolkit-venv"
"$HOME/toolkit-venv/bin/python" -m pip install /absolute/path/to/poundlock_toolkit-0.4.0-py3-none-any.whl
"$HOME/toolkit-venv/bin/poundlock-toolkit" inspect
```

Copy `examples/target-local.json` or `examples/target-ssh.json` to private storage.
Set `python` to the target's absolute environment executable. SSH `host` is a
normal alias already configured in your SSH client; username, key and port belong
in that client's configuration. No passwords, private keys or forwarding flags
belong in Toolkit JSON. Establish new host trust manually after verifying its
fingerprint through a trusted channel. Toolkit uses strict host checking and batch
mode, disables agent/port forwarding and never bypasses a trust failure or runs sudo.
Trusted SSH configuration may contain your existing ProxyJump/ProxyCommand choices;
Toolkit does not create or sanitize that separately trusted operator configuration.

```sh
poundlock-toolkit target-inspect private/target.json --output private/inventory.json
poundlock-toolkit target-config private/target.json private/endpoint.json --output private/target-evaluation.json
poundlock-toolkit target-plan private/target.json private/target-evaluation.json --output private/target-plan.json
poundlock-toolkit target-run private/target.json private/target-evaluation.json --plan private/target-plan.json --receipt private/receipt.json --output private/result.json --execute
```

`target-config` explicitly copies observed target OS/architecture/memory into a new
configuration and accepts only loopback endpoints. For endpoints elsewhere, use
`source: operator` with separately verified inventory, or preserve unknowns.
Review the bound target fingerprint, target inventory, destination, budgets and
configuration fingerprint in the plan. Changing configuration requires a new plan.

A `memory_probe` in the configuration is read on the target, next to the server, so
its `pid` is the target's and a `process` name is looked up among the target's
processes. Nothing is read from the client, and no extra privileges or transfers are
involved; the sampled values come back inside the saved result. The evaluation
still refuses a non-loopback endpoint for it.

## Attach-time checks and drift

`target-run` rechecks the reviewed plan before creating any run state, and the
target agent repeats the checks against itself at attach time. Each check reports
`pass`, `fail`, `unknown` or `stale`:

| Check | Stale or failing when |
|---|---|
| `implementation` | Package bytes differ from those the plan was reviewed with |
| `target` | The target configuration differs from the reviewed one |
| `plan_version` | The plan predates this package's plan format |
| `reviewed_inputs` | A configuration section changed; `drifted` names it (`model`, `backend_build`, `serving_application`, `settings`, `harness`, `inventory`, `limits`, ...) |
| `stable_inventory` | OS, architecture, CPU count, total/unified memory, GPU devices or pool sizes, or relevant executables changed; `drifted` lists each assumption with reviewed and observed values. A value that was known at review and is now unreadable is `unknown` |
| `capacity` | Current free memory is below `requirements.min_available_memory_bytes` (`fail`), or cannot be read while required (`unknown`) |

Free memory and timestamps never invalidate stable identity; capacity is only
compared with the configured requirement. Any non-passing check exits with code 4
and prints `next_required_check` plus the next step (usually: re-run `target-plan`
and review it). A refused attach releases the receipt it reserved, because the
target created no run state. Endpoint URLs and credential names are not digested
per section; an endpoint-only change is reported as `endpoint_or_credential_reference`.

## Disconnect, status, cancellation and removal

A run reserves a private local receipt before sending its creation request. Keep
that receipt: it binds target, package, configuration, the reviewed plan's SHA-256,
unpredictable run ID and ownership token. Status, cancel and removal refuse a
receipt whose package or reviewed plan differs from the run's. Target state is isolated under
`~/.local/state/poundlock-toolkit/runs/<run-id>/` with private permissions. Status
uses that same ID and never starts an evaluation. Replaying a creation ID fails.
A missing receipt's target run cannot be reconstructed from an endpoint name.

```sh
poundlock-toolkit target-status private/target.json private/receipt.json --output private/retrieved.json
poundlock-toolkit target-cancel private/target.json private/receipt.json --execute
poundlock-toolkit target-status private/target.json private/receipt.json --output private/final.json
poundlock-toolkit target-remove private/target.json private/receipt.json --execute
```

Use a new output filename when retrieving again. The transferred document is schema
validated and its SHA-256 checked. Cancellation returns current status; check again
for a terminal state. Remove refuses active runs, unresolved container cleanup and
unexpected artifact links. Retrieve evidence before removing that exact run directory.
No service, other run, model, account configuration or shared runtime is removed.

The transport-loss policy is **cancel**. EOF or a closed SSH channel requests target
cancellation; request/total deadlines still apply if the channel fails silently.
SSH keepalives bound client detection. The target's independent watchdog stops its
worker by total deadline +35 seconds; attached cleanup waits at most 30 seconds.
Pi's own container watchdog bounds task work independently of a vanished parent.
Allow total deadline +40 seconds before treating stale state as interrupted.
Stopped containers may remain after hard failure; inspect the exact ownership
journal and settle cleanup before removal. No PID/name-pattern sweep is performed.
A target crash/power loss cannot guarantee successful cleanup or evidence writes.

Keep the tested environment installed until its receipts are settled: later package
bytes are intentionally rejected. Remote secret variables are not forwarded; direct
authenticated checks require an already authorized target-side environment. Setup
scripts are a separate explicit step, never an automatic effect of target-run.

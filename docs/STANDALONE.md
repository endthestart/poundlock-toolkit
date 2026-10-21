# Standalone isolated CPU and Pi recipe

This recipe needs no existing model server. It creates a temporary CPU-only
llama.cpp server, an internal Docker network and disposable Pi/verifier containers.
The host API listens only on 127.0.0.1; Pi reaches `inference:8080` inside that network.
No endpoint credential is placed in model-readable tools. Existing services are
never adopted or stopped. The complete recipe still needs observed live qualification;
script/adapter tests alone do not establish that an arbitrary model supports tools.

## Explicit prerequisites

On the **target**, install Python 3.11+ and the tested Toolkit wheel as described in
[TARGETS](TARGETS.md). Install Docker through its official platform instructions
within your authority. Linux requires a running Docker engine with CPU, memory,
swap and process limits; macOS requires an already configured Docker VM with enough
memory. Toolkit rejects remote Docker engines. Docker administration grants powerful
machine access; use your existing authorized setup rather than silently changing
account groups or using sudo in the recipe.

Prepare these artifacts before generating a setup plan:

- A local GGUF, its verified publisher revision/license and SHA-256. Record the file
  size and review the download before fetching it. Weight bytes are not total memory;
  context/cache/backend overhead must fit the selected server limit.
- A reviewed, digest-pinned CPU llama.cpp server image containing `/app/llama-server`
  and `/usr/bin/timeout`. The upstream [Docker documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)
  describes CPU server images for amd64 and arm64. Check the chosen image and
  license; no GPU/driver setup is part of this recipe.
- A reviewed, digest-pinned image containing Python 3 and **Pi 1.1.0**.
  [Pi upstream instructions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
  describe installation. `recipes/Pi.Dockerfile` is a build recipe with an explicit
  pinned Node base argument. Review base size and package resolution, then build
  using at most the authorized CPU/memory budget and record the resulting digest.
  Toolkit neither pulls nor builds images during evaluation.

Verify exact installed versions and image contents before model execution:

```sh
docker image inspect REGISTRY/server@sha256:VERIFIED_DIGEST
docker run --rm --network none --entrypoint /usr/bin/timeout REGISTRY/server@sha256:VERIFIED_DIGEST --version
docker run --rm --network none --entrypoint /app/llama-server REGISTRY/server@sha256:VERIFIED_DIGEST --version
docker run --rm --network none --entrypoint pi REGISTRY/pi@sha256:VERIFIED_DIGEST --version
docker run --rm --network none --entrypoint python3 REGISTRY/pi@sha256:VERIFIED_DIGEST --version
```

These are operator-reviewed prerequisite checks, not automatic Toolkit actions.
Replace every uppercase placeholder. A locally built image must have a registry
RepoDigest before use; promoting it to your authorized internal registry is a
separate explicit build step. Never substitute a mutable tag for the reviewed digest.
The Pi adapter checks the actual CLI version at run time. The 0.84.4, 0.85.0, 0.85.1,
1.0.0, 1.0.3 and 1.1.0 interfaces and live model evidence remain separate qualification records.

## Review, run and remove

After `mkdir -m 700 private`, write
`poundlock-toolkit example standalone --output private/standalone.json`. Replace absolute target Python/model
paths, model checksum, both image digests, exact model/backend/template identity,
settings and target inventory. The all-zero digests are deliberately unusable.
Defaults allocate a 4 GiB CPU server, three server CPUs and one 512 MiB/one CPU Pi
worker; plan for their concurrent total plus Docker/host overhead. The example has
one task repetition, so it demonstrates mechanics and cannot meet repeated-run
qualification. Increase repetitions only in a new bounded reviewed plan.

```sh
poundlock-toolkit setup-plan private/standalone.json --output private/setup-plan.json
poundlock-toolkit setup-script private/standalone.json --plan private/setup-plan.json --output private/reviewed-setup.sh
# Read the complete plan and script before this explicit machine-changing action:
sh private/reviewed-setup.sh
```

For SSH, generate with target paths, review locally, then explicitly execute on the
selected trusted target with your normal SSH client, e.g.
`ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes evaluation-host sh -s < private/reviewed-setup.sh`.
No credentials are embedded. The script prints its private target evidence directory
immediately after creation, verifies model bytes and local Docker limits, creates
only randomly named owned resources, and records cleanup. It downloads zero bytes.
Every Docker client call is time bounded; server lifetime is capped independently
inside the container. Ctrl-C/SSH loss requests cleanup. The maximum configured
server lifetime includes readiness, evaluation and cleanup margin. After a hard
client loss the server exits by lifetime +10 seconds, but its stopped container or
network may require exact manual cleanup from `server.cid` and the plan's network
name after verifying its `poundlock-toolkit.run` label. Never prune unrelated Docker
resources. Keep failure evidence, including `setup-cleanup.json`.

The script runs the same evaluator locally on the selected target and retains
configuration, plan, result, Markdown report and allowlisted export. For reconnectable
transport evaluation of an already prepared endpoint, use `target-run` in TARGETS;
setup itself is a finite reviewed shell action, not a durable remote setup service.
Transfer the chosen private evidence files through trusted SSH and verify checksums.
Delete only that printed evidence directory after reviewing and retaining reports.
Image/model removal is optional manual housekeeping; they may be shared and are
never owned by this run. Toolkit's [removal guide](OPERATIONS.md) covers the package.

Readiness, API, forced-tool and streaming gates precede Pi. Passing the slug repair
requires actual tool execution/completion and an independent network-disabled
verifier. Inspect failure denominators and unknown identity before interpreting a
recommendation. A second configuration must have its own reviewed plan and result.

The fixed task now enforces its tool scope: read/write/edit address only the two
work files (read also permits the verifier), and bash runs only the exact protected
verifier command. The model cannot launch a second client or edit Pi's provider or
extension through these tools. This is a narrow coding fixture, not general shell
agent qualification. The Docker boundary remains mandatory.

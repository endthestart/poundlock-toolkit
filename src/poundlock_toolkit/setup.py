"""Render a reviewed, finite standalone recipe; never install or launch implicitly."""

import json
import os
from pathlib import Path
import re
import shlex
import uuid

from .core import digest, read_json, validate


def setup_spec(path):
    spec = read_json(path)
    required = {
        "python",
        "model_path",
        "model_sha256",
        "server_image",
        "pi_image",
        "pi_version",
        "port",
        "server_memory_mb",
        "cpus",
        "lifetime_seconds",
        "evaluation",
    }
    if set(spec) != required:
        raise ValueError("Invalid standalone specification fields")
    for key in ("python", "model_path"):
        if (
            not isinstance(spec[key], str)
            or not spec[key].startswith("/")
            or any(ord(x) < 32 for x in spec[key])
        ):
            raise ValueError("Use absolute target paths")
    for key in ("server_image", "pi_image"):
        if not isinstance(spec[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", spec[key]
        ):
            raise ValueError("Images must be digest pinned")
    if not re.fullmatch("[0-9a-f]{64}", str(spec["model_sha256"])):
        raise ValueError("Expected model checksum required")
    if spec["pi_version"] != "1.1.0":
        raise ValueError("Pi version outside reviewed scope")
    for key, minimum, maximum in [
        ("port", 1024, 65535),
        ("server_memory_mb", 512, 32768),
        ("cpus", 1, 4),
        ("lifetime_seconds", 60, 1200),
    ]:
        if type(spec[key]) is not int or not minimum <= spec[key] <= maximum:
            raise ValueError("Standalone resource limit out of range")
    validate(spec["evaluation"], "config.schema.json")
    c = spec["evaluation"]
    if (
        c["credential_env"] is not None
        or c["paid"] is not False
        or c["data_destination"] != "local"
    ):
        raise ValueError("Recipe is isolated local unpaid inference only")
    if (
        c["harness"]["name"] != "pi"
        or c["api"] != "openai"
        or "coding" not in c["workloads"]
    ):
        raise ValueError("Recipe requires Pi coding workload")
    if spec["lifetime_seconds"] < c["limits"]["total_seconds"] + 60:
        raise ValueError("Server lifetime must cover evaluation and readiness budget")
    return spec


def setup_plan(spec):
    return {
        "setup_version": 1,
        "spec_sha256": digest(spec),
        "run_id": uuid.uuid4().hex,
        "mode": "manual-reviewed-script",
        "downloads_bytes": 0,
        "privileged_steps": [],
        "prerequisites": [
            "Existing local Docker with CPU/memory/pids/swap enforcement",
            "Existing pinned CPU llama.cpp server image with /app/llama-server and /usr/bin/timeout",
            "Existing pinned Pi/Python image and readable GGUF with expected SHA-256",
            "Same tested Toolkit installed in the specified target Python environment",
        ],
        "scope": {
            "network": "new owned internal Docker network",
            "listen": "127.0.0.1 only",
            "server_cpus": spec["cpus"],
            "server_memory_mb": spec["server_memory_mb"],
            "maximum_server_seconds": spec["lifetime_seconds"],
            "model_path": spec["model_path"],
        },
        "evaluation_limits": spec["evaluation"]["limits"],
        "ownership": "Only the randomly named network/server created by this script; no attached resources touched",
    }


def setup_script(spec, plan):
    expected = setup_plan(spec)
    expected["run_id"] = plan.get("run_id")
    if plan != expected or not re.fullmatch("[0-9a-f]{32}", str(plan.get("run_id"))):
        raise ValueError("Setup plan mismatch")
    # Everything provided by the user is encoded as shell words or JSON literals.
    run = plan["run_id"]
    network = "toolkit-net-" + run
    server = "toolkit-server-" + run
    c = json.loads(json.dumps(spec["evaluation"]))
    c["endpoint"] = f"http://127.0.0.1:{spec['port']}/v1"
    c["harness"].update(
        image=spec["pi_image"],
        version=spec["pi_version"],
        network=network,
        endpoint="http://inference:8080/v1",
    )
    model = str(spec["model_path"])
    # --mount source supports commas specially, so reject them rather than guessing escaping.
    if "," in model:
        raise ValueError("Model mount path must not contain a comma")
    server_cmd = [
        "docker",
        "run",
        "--detach",
        "--pull=never",
        "--name",
        server,
        "--cidfile",
        "server.cid",
        "--label",
        "poundlock-toolkit.run=" + run,
        "--network",
        network,
        "--network-alias",
        "inference",
        "--publish",
        f"127.0.0.1:{spec['port']}:8080",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus",
        str(spec["cpus"]),
        "--memory",
        f"{spec['server_memory_mb']}m",
        "--memory-swap",
        f"{spec['server_memory_mb']}m",
        "--pids-limit",
        "128",
        "--log-driver=none",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "--mount",
        f"type=bind,source={model},target=/model/weights.gguf,readonly",
        "--entrypoint",
        "/usr/bin/timeout",
        spec["server_image"],
        "--signal=TERM",
        "--kill-after=10",
        str(spec["lifetime_seconds"]),
        "/app/llama-server",
        "--model",
        "/model/weights.gguf",
        "--alias",
        c["model"]["id"],
        "--ctx-size",
        str(c["settings"]["context_tokens"]),
        "--threads",
        str(spec["cpus"]),
        "--n-gpu-layers",
        "0",
        "--parallel",
        "1",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
        "--jinja",
    ]
    verify = 'import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); h=hashlib.file_digest(p.open("rb"),"sha256").hexdigest(); sys.exit(0 if h==sys.argv[2] else 1)'
    write = 'import json,pathlib,sys; pathlib.Path("evaluation.json").write_text(sys.argv[1])'
    ready = (
        "import time,urllib.request; from poundlock_toolkit.backends import NoRedirect; opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()); u="
        + repr(c["endpoint"] + "/models")
        + '; end=time.monotonic()+30\nwhile time.monotonic()<end:\n try:\n  opener.open(u,timeout=1).close(); break\n except OSError: time.sleep(1)\nelse: raise SystemExit("server readiness timeout")'
    )
    python = spec["python"]
    q = shlex.quote
    lines = [
        "#!/bin/sh",
        "set -eu",
        "umask 077",
        "# Reviewed standalone CPU recipe. No downloads, installs, sudo or production service changes.",
        "network_id=",
        "workdir=",
        "cleanup() {",
        "  prior=$?; trap - EXIT HUP INT TERM; failed=0",
        '  if [ -n "$workdir" ] && [ -s "$workdir/server.cid" ]; then docker rm -f "$(cat "$workdir/server.cid")" >/dev/null 2>&1 || failed=1; fi',
        '  if [ -n "$network_id" ]; then docker network rm "$network_id" >/dev/null 2>&1 || failed=1; fi',
        '  if [ -n "$workdir" ]; then printf \'{"cleanup_failed":%s}\\n\' "$failed" > "$workdir/setup-cleanup.json"; fi',
        '  if [ "$failed" = 1 ]; then echo "Owned cleanup incomplete; inspect retained setup evidence" >&2; exit 2; fi',
        '  exit "$prior"',
        "}",
        "trap cleanup EXIT",
        "trap 'exit 130' HUP INT TERM",
        shlex.join(
            [
                python,
                "-I",
                "-B",
                "-c",
                "from poundlock_toolkit.pi import local_engine_preflight; local_engine_preflight()",
            ]
        ),
        shlex.join([python, "-I", "-B", "-c", verify, model, spec["model_sha256"]]),
        shlex.join(["docker", "image", "inspect", spec["server_image"]])
        + " >/dev/null",
        shlex.join(["docker", "image", "inspect", spec["pi_image"]]) + " >/dev/null",
        f'if docker container inspect {q(server)} >/dev/null 2>&1 || docker network inspect {q(network)} >/dev/null 2>&1; then echo "Owned names already exist; inspect previous run" >&2; exit 2; fi',
        f'workdir=$(mktemp -d "${{TMPDIR:-/tmp}}/toolkit-standalone-{run}.XXXXXX")',
        'printf "Retained private evidence: %s\\n" "$workdir"',
        'cd "$workdir"',
        shlex.join([python, "-I", "-B", "-c", write, json.dumps(c)]),
        shlex.join(
            [
                python,
                "-I",
                "-B",
                "-m",
                "poundlock_toolkit",
                "plan",
                "evaluation.json",
                "--output",
                "plan.json",
            ]
        ),
        "network_id=$("
        + shlex.join(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--label",
                "poundlock-toolkit.run=" + run,
                network,
            ]
        )
        + ")",
        shlex.join(server_cmd) + " >/dev/null 2>&1",
        shlex.join([python, "-I", "-B", "-c", ready]),
        "result=0",
        shlex.join(
            [
                python,
                "-I",
                "-B",
                "-m",
                "poundlock_toolkit",
                "evaluate",
                "evaluation.json",
                "--plan",
                "plan.json",
                "--execute",
                "--output",
                "result.json",
            ]
        )
        + " || result=$?",
        shlex.join(
            [
                python,
                "-I",
                "-B",
                "-m",
                "poundlock_toolkit",
                "recommend",
                "result.json",
                "--output",
                "report.md",
            ]
        ),
        shlex.join(
            [
                python,
                "-I",
                "-B",
                "-m",
                "poundlock_toolkit",
                "export",
                "result.json",
                "--output",
                "qualification.json",
            ]
        ),
        'printf "Retained private evidence: %s\\n" "$workdir"',
        'exit "$result"',
    ]
    # Bound every Docker client call, including cleanup; server work has its own
    # independent in-container timeout if the setup client disappears.
    wrapper = shlex.join(
        [
            python,
            "-I",
            "-B",
            "-c",
            'import sys; from poundlock_toolkit.pi import bounded; r,o=bounded(["docker"]+sys.argv[1:],15); sys.stdout.buffer.write(o); sys.exit(r)',
        ]
    )
    lines.insert(3, "docker() { " + wrapper + ' "$@"; }')
    return "\n".join(lines) + "\n"


def arguments(sub):
    p = sub.add_parser(
        "setup-plan",
        help="Review one isolated standalone CPU recipe without executing it",
    )
    p.add_argument("spec")
    p.add_argument("--output", required=True)
    p = sub.add_parser(
        "setup-script",
        help="Render exact reviewed setup/evaluation/removal commands; no execution",
    )
    p.add_argument("spec")
    p.add_argument("--plan", required=True)
    p.add_argument("--output", required=True)


def cli(args):
    from .core import atomic_json

    spec = setup_spec(args.spec)
    if Path(args.output).exists():
        raise ValueError("Output already exists")
    if args.command == "setup-plan":
        result = setup_plan(spec)
        atomic_json(args.output, result)
        print(json.dumps(result, indent=2))
    else:
        text = setup_script(spec, read_json(args.plan))
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        print(
            "Review the rendered script before explicitly executing it on the selected target. No setup changes were made."
        )
    return 0

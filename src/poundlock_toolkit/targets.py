"""Finite local/SSH transport. Arguments are data; SSH retains normal host trust."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import time
import uuid

from .core import atomic_json, digest, read_json, validate
from .drift import (
    DriftError,
    check,
    evaluation_check,
    require,
    sanitize,
    stable_inventory,
)

PROTOCOL = 1
TARGET_PLAN_VERSION = 2
MAX_PACKET = 4_000_000


def implementation_id():
    root = Path(__file__).parent
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and (p.suffix in (".py", ".json", ".ts"))
        and "__pycache__" not in p.parts
    }
    return digest(hashes)


def target_config(path):
    value = read_json(path)
    if set(value) != {"transport", "host", "python"}:
        raise ValueError("Target needs exactly transport, host and python")
    if value["transport"] not in ("local", "ssh"):
        raise ValueError("Unsupported target transport")
    python = value["python"]
    if (
        not isinstance(python, str)
        or not python.startswith("/")
        or len(python) > 1024
        or any(ord(c) < 32 for c in python)
    ):
        raise ValueError("Use an absolute target Python executable path")
    if value["transport"] == "ssh":
        if not isinstance(value["host"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value["host"]
        ):
            raise ValueError("Use a normal SSH configuration host alias")
    elif value["host"] is not None:
        raise ValueError("A local target has no SSH host")
    return value


def command(target):
    argv = [target["python"], "-I", "-B", "-m", "poundlock_toolkit.target_agent"]
    if target["transport"] == "local":
        return argv
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "--",
        target["host"],
        shlex.join(argv),
    ]


class TransportError(RuntimeError):
    pass


def exchange(target, packet, timeout=20):
    """Stdin remains open while run is attached; closing it requests cancellation."""
    payload = (
        json.dumps(dict(packet, protocol=PROTOCOL), allow_nan=False).encode() + b"\n"
    )
    if len(payload) > MAX_PACKET:
        raise ValueError("Request too large")
    # A local agent is Toolkit itself and reads the endpoint key credential_env names;
    # ssh needs the agent socket and the owner's ssh configuration.
    # nosemgrep: toolkit-py-subprocess-inherits-environment
    process = subprocess.Popen(
        command(target),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    raw = bytearray()
    try:
        deadline = time.monotonic() + timeout
        os.set_blocking(process.stdin.fileno(), False)
        sent = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TransportError(
                        "Target timeout; reconnect with the saved receipt"
                    )
                for key, _ in selector.select(0.1):
                    if key.fileobj is process.stdin:
                        try:
                            sent += os.write(key.fd, payload[sent : sent + 4096])
                        except BlockingIOError:
                            continue
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        raw.extend(chunk)
                    if len(raw) > MAX_PACKET:
                        raise TransportError("Target response exceeds limit")
        status = process.wait(timeout=2)
        if status == 2 and raw:
            # The agent's only structured refusal: a named attach check failed
            # before any run state was created.
            refusal = json.loads(raw)
            if (
                isinstance(refusal, dict)
                and refusal.get("code") == "attach_checks_failed"
            ):
                raise DriftError(sanitize(refusal.get("checks")))
        if status != 0:
            raise TransportError(
                "Target unavailable or trust/prerequisite check failed"
            )
        response = json.loads(raw)
        if response.get("protocol") != PROTOCOL or not response.get("ok"):
            raise TransportError(
                "Target refused request; check prerequisites and saved receipt"
            )
        if response.get("implementation") != implementation_id():
            raise TransportError(
                "Client/target package bytes differ; install the same tested artifact"
            )
        if response.get("result") is not None:
            validate(response["result"])
            if digest(response["result"]) != response.get("result_sha256"):
                raise TransportError("Transferred result checksum differs")
        return response
    except DriftError:
        raise
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise TransportError(
            "Transport failed; private error details suppressed"
        ) from exc
    finally:
        process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()


def inventory(target):
    return exchange(
        target, {"operation": "inspect", "implementation": implementation_id()}
    )


def target_plan(c, target, readiness_only=False):
    from .planning import plan

    observed = inventory(target)
    return {
        "target_plan_version": TARGET_PLAN_VERSION,
        "target_sha256": digest(target),
        "implementation": implementation_id(),
        "target_inventory": observed["inventory"],
        "stable_inventory_sha256": digest(stable_inventory(observed["inventory"])),
        "evaluation": plan(c, readiness_only),
        "disconnect_policy": "cancel",
        "maximum_cleanup_grace_seconds": 40,
    }


def plan_checks(c, target, reviewed):
    """Client-side checks that need no target access; run before any receipt."""
    from .planning import plan

    checks = [
        check(
            "implementation",
            "pass"
            if reviewed.get("implementation") == implementation_id()
            else "stale",
        ),
        check(
            "target",
            "pass" if reviewed.get("target_sha256") == digest(target) else "stale",
        ),
    ]
    if reviewed.get("target_plan_version") != TARGET_PLAN_VERSION:
        return checks + [check("plan_version", "stale")]
    if (
        reviewed.get("disconnect_policy") != "cancel"
        or reviewed.get("maximum_cleanup_grace_seconds") != 40
    ):
        raise ValueError("Target lifecycle plan changed")
    if reviewed.get("stable_inventory_sha256") != digest(
        stable_inventory(reviewed["target_inventory"])
    ):
        raise ValueError("Reviewed target inventory was edited after planning")
    evaluation = reviewed["evaluation"]
    return checks + [
        evaluation_check(evaluation, plan(c, evaluation.get("readiness_only") is True))
    ]


def start(c, target, reviewed, receipt_path):
    require(plan_checks(c, target, reviewed))
    if reviewed["evaluation"]["blockers"]:
        raise ValueError("Plan is blocked")
    # Reserve the receipt before any remote mutation. Retry/status always use its ID.
    # The receipt binds the run to the reviewed plan, inputs and package bytes.
    receipt = {
        "protocol": PROTOCOL,
        "run_id": str(uuid.uuid4()),
        "token": uuid.uuid4().hex,
        "target_sha256": digest(target),
        "implementation": implementation_id(),
        "config_sha256": digest(c),
        "plan_sha256": digest(reviewed),
    }
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(receipt, f)
        f.flush()
        os.fsync(f.fileno())
    try:
        response = exchange(
            target,
            dict(receipt, operation="run", config=c, plan=reviewed),
            c["limits"]["total_seconds"] + 45,
        )
        response["attach_checks"] = sanitize(response.get("attach_checks"))
        return response
    except DriftError:
        # The target refused before creating run state; this receipt owns nothing.
        os.unlink(receipt_path)
        raise


def control(target, receipt, operation):
    if receipt.get("target_sha256") != digest(target):
        raise ValueError("Receipt belongs to another target")
    if operation not in ("status", "cancel", "remove"):
        raise ValueError("Unknown control operation")
    if receipt.get("implementation") != implementation_id():
        # Only the package that created the run may read or change it.
        require([check("implementation", "stale")])
    response = exchange(target, dict(receipt, operation=operation), 30)
    binding = response.get("binding")
    if operation != "remove" and binding != {
        k: receipt.get(k) for k in ("config_sha256", "plan_sha256")
    }:
        raise TransportError("Target run is not bound to this receipt's reviewed plan")
    return response


def save_result(response, path):
    if response.get("result") is None:
        return False
    if Path(path).exists():
        raise ValueError("Output exists; original evidence preserved")
    atomic_json(path, response["result"])
    return True

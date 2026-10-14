"""One finite target invocation; no listener, daemon, queue or runtime controller."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from .core import atomic_json, config, digest, now, read_json, validate
from .evaluation import evaluate, recover
from .inspection import inspect
from .drift import (
    DriftError,
    capacity_check,
    check,
    evaluation_check,
    inventory_check,
    require,
)
from .targets import MAX_PACKET, PROTOCOL, TARGET_PLAN_VERSION, implementation_id


def state_root(create=False):
    root = Path.home() / ".local" / "state" / "poundlock-toolkit" / "runs"
    # Reject existing symlinks throughout the owned path, including ancestor aliases.
    for p in [root, *root.parents]:
        if p.is_symlink():
            raise ValueError("Symlink state path")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.exists():
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("State directory must be private and owned")
    return root


def run_root(run_id, create=False):
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", run_id
    ):
        raise ValueError("Invalid run ID")
    root = state_root(create) / run_id
    if root.is_symlink():
        raise ValueError("Symlink run path")
    if create:
        root.mkdir(mode=0o700)  # exclusive: a second run request never launches twice
    return root


def authorize(packet):
    root = run_root(packet["run_id"])
    marker = read_json(root / "owner.json")
    if not hmac.compare_digest(marker["token"], str(packet.get("token", ""))):
        raise ValueError("Wrong ownership token")
    if any(packet.get(k) != marker.get(k) for k in ("plan_sha256", "config_sha256")):
        raise ValueError("Receipt is bound to another reviewed plan")
    return root, marker


def summary(root, marker):
    state = read_json(root / "state.json")
    result = (
        read_json(root / "result.json") if (root / "result.json").is_file() else None
    )
    if state["status"] == "running" and time.time() > marker["deadline_epoch"] + 40:
        state = {
            "status": "interrupted",
            "updated_at": datetime.fromtimestamp(
                marker["deadline_epoch"] + 40, timezone.utc
            ).isoformat(),
        }
    if state["status"] != "running" and result is not None:
        pending = [r["id"] for r in result["attempts"] if r["outcome"] == "running"]
        result = recover(result)
        for row in result["attempts"]:
            if row["id"] in pending:
                row["completed_at"] = state["updated_at"]
    return {
        "state": state,
        "result": result,
        "result_sha256": digest(result) if result is not None else None,
        "binding": {k: marker.get(k) for k in ("config_sha256", "plan_sha256")},
    }


def worker(run_id):
    root = run_root(run_id)
    marker = read_json(root / "owner.json")
    c = config(root / "config.json")
    stopped = threading.Event()

    def cancel(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, cancel)

    def watch():
        while not stopped.wait(0.1):
            if (root / "cancel").exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=watch, daemon=True).start()

    # An independent hard backstop survives loss of SSH and of the attached parent.
    def hard_stop():
        atomic_json(root / "state.json", {"status": "interrupted", "updated_at": now()})
        os._exit(124)

    watchdog = threading.Timer(
        max(0.1, marker["deadline_epoch"] - time.time() + 35), hard_stop
    )
    watchdog.daemon = True
    watchdog.start()
    terminal = "failed"
    try:
        d = evaluate(c, root / "result.json", readiness_only=marker["readiness_only"])
        terminal = (
            "completed"
            if all(r["outcome"] == "pass" for r in d["attempts"])
            else "cancelled"
            if any(r["outcome"] == "cancelled" for r in d["attempts"])
            else "failed"
        )
    except KeyboardInterrupt:
        terminal = "cancelled"
    finally:
        stopped.set()
        watchdog.cancel()
        atomic_json(root / "state.json", {"status": terminal, "updated_at": now()})


def attached_run(packet):
    from .planning import plan

    c = packet["config"]
    validate(c, "config.schema.json")
    reviewed = packet["plan"]
    if packet.get("plan_sha256") != digest(reviewed) or packet.get(
        "config_sha256"
    ) != digest(c):
        raise ValueError("Receipt is not bound to the supplied plan/config")
    if reviewed.get("target_plan_version") != TARGET_PLAN_VERSION:
        require([check("plan_version", "stale")])
    readonly = reviewed["evaluation"]["readiness_only"]
    # Recheck on the target itself at attach time: reviewed inputs and package,
    # stable decision-relevant inventory, then current capacity separately.
    observed = inspect()
    checks = require(
        [
            check(
                "implementation",
                "pass"
                if reviewed["implementation"] == implementation_id()
                else "stale",
            ),
            evaluation_check(reviewed["evaluation"], plan(c, readonly)),
            inventory_check(reviewed["target_inventory"], observed),
            capacity_check(c, observed),
        ]
    )
    if reviewed["evaluation"]["blockers"]:
        raise ValueError("Blocked plan")
    if not isinstance(packet.get("token"), str) or not re.fullmatch(
        "[0-9a-f]{32}", packet["token"]
    ):
        raise ValueError("Invalid ownership token")
    root = run_root(packet["run_id"], True)
    marker = {
        "token": packet["token"],
        "run_id": packet["run_id"],
        "deadline_epoch": time.time() + c["limits"]["total_seconds"],
        "readiness_only": readonly,
        "implementation": implementation_id(),
        "plan_sha256": packet["plan_sha256"],
        "config_sha256": packet["config_sha256"],
    }
    atomic_json(root / "owner.json", marker)
    atomic_json(root / "config.json", c)
    atomic_json(root / "state.json", {"status": "running", "updated_at": now()})
    child = None
    disconnect = threading.Event()

    def disconnected(*_):
        disconnect.set()

    signal.signal(signal.SIGTERM, disconnected)
    signal.signal(signal.SIGHUP, disconnected)
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "poundlock_toolkit.target_agent",
                "--worker",
                packet["run_id"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while child.poll() is None:
                for key, _ in selector.select(0.1):
                    if not os.read(key.fd, 1024):
                        disconnect.set()
                        selector.unregister(key.fileobj)
                if disconnect.is_set() or time.time() > marker["deadline_epoch"]:
                    (root / "cancel").touch(mode=0o600, exist_ok=True)
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    break
        state = read_json(root / "state.json")
        if state["status"] == "running":
            atomic_json(
                root / "state.json", {"status": "interrupted", "updated_at": now()}
            )
        return dict(summary(root, marker), attach_checks=checks)
    finally:
        if child is not None and child.poll() is None:
            (root / "cancel").touch(mode=0o600, exist_ok=True)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def dispatch(packet):
    if (
        packet.get("protocol") != PROTOCOL
        or packet.get("implementation") != implementation_id()
    ):
        raise ValueError("Package/protocol mismatch")
    op = packet["operation"]
    if op == "inspect":
        return {"inventory": inspect()}
    if op == "run":
        return attached_run(packet)
    root, marker = authorize(packet)
    if op == "cancel":
        (root / "cancel").touch(mode=0o600, exist_ok=True)
    if op in ("status", "cancel"):
        return summary(root, marker)
    if op == "remove":
        s = summary(root, marker)
        if s["state"]["status"] == "running":
            raise ValueError("Cancel and settle before removal")
        # Never erase a cleanup journal with unresolved owned containers.
        if any(
            read_json(p).get("cleanup") != "passed" for p in root.glob("*.owned.json")
        ):
            raise ValueError("Owned resource cleanup unresolved")
        if any(
            p.is_symlink() or (not p.is_file() and not p.is_dir())
            for p in root.rglob("*")
        ):
            raise ValueError("Unexpected artifact path")
        shutil.rmtree(root)
        return {"state": {"status": "removed"}}
    raise ValueError("Unknown operation")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
        return 0
    try:
        raw = sys.stdin.buffer.readline(MAX_PACKET + 1)
        if len(raw) > MAX_PACKET:
            raise ValueError("Packet too large")
        result = dispatch(json.loads(raw))
        print(
            json.dumps(
                dict(
                    result,
                    ok=True,
                    protocol=PROTOCOL,
                    implementation=implementation_id(),
                ),
                allow_nan=False,
            ),
            flush=True,
        )
        return 0
    except DriftError as drift:
        print(
            json.dumps(
                {
                    "ok": False,
                    "protocol": PROTOCOL,
                    "code": "attach_checks_failed",
                    "checks": drift.checks,
                },
                allow_nan=False,
            ),
            flush=True,
        )
        return 2
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        print(
            json.dumps(
                {"ok": False, "protocol": PROTOCOL, "code": "target_request_failed"}
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

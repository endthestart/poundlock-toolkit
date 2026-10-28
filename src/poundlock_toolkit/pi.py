"""Owned Docker Pi workers with protected, separately executed verification."""

from __future__ import annotations
import hashlib
import io
import json
import os
import re
import selectors
import signal
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from .core import Failure, atomic_json, child_env, data, digest
from .backends import empty_metrics


_LEADING_CD = re.compile(r"^cd\s+\S+\s*&&\s*")


def canonical_call(event):
    """A tool call reduced to what makes two calls "the same attempt".

    Adapted from an earlier research harness's loop probes: a `bash` command is compared
    after stripping an incidental leading `cd X &&`, so re-running the same
    command from the same directory in two different ways still matches.
    Every other tool compares by name and its sorted-key JSON arguments.
    """
    name = event.get("toolName")
    args = event.get("args") or {}
    if name == "bash":
        command = _LEADING_CD.sub("", str(args.get("command", "")).strip())
        return ("bash", command)
    try:
        return (name, json.dumps(args, sort_keys=True))
    except (TypeError, ValueError):
        return (name, repr(args))


def count_compactions(events):
    """Completed Pi context compactions in a transcript.

    Counts `compaction_end`, never `compaction_start`: an interrupted or
    still-running compaction is not evidence that history was actually
    summarized away. Pi also emits `compaction_end` for a compaction that was
    aborted or failed (`aborted`, `errorMessage`); that one summarized nothing,
    so it is not counted either.
    """
    n = 0
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            event.get("type") == "compaction_end"
            and not event.get("aborted")
            and not event.get("errorMessage")
        ):
            n += 1
    return n


def active_compaction_gate(c, workload):
    """The forced-compaction settings `workload` runs under, or None.

    Opt-in through `harness.compaction_gate.enabled`, and only ever for
    coding: investigation has no equivalent lab finding, so it keeps the
    default of compaction disabled. The runner and the qualification document
    both read this, so what was run and what is recorded cannot disagree.
    """
    gate = c["harness"].get("compaction_gate")
    return gate if gate and gate["enabled"] and workload == "coding" else None


def count_loops(events, window=8, max_period=4, distinct_threshold=3):
    """Repeated/cyclical tool calls in a transcript, adapted from an earlier
    research harness's `coarse_target`/`cycle_period` probes to this fixture's
    small (12-request) turn budget: a shorter window and smaller periods are
    enough to catch a stuck agent here without that harness's larger polling
    window.

    A window of the most recent calls is periodic (period 1..max_period) when
    every call equals the one exactly `period` positions earlier; a full
    window collapsing to `distinct_threshold` or fewer distinct calls is
    flagged even without exact periodicity. Each newly-completed window is
    checked once, so a single stuck stretch is not recounted every call.
    """
    calls = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "tool_execution_start":
            calls.append(canonical_call(event))
    loops, flagged_before = 0, False
    for end in range(2, len(calls) + 1):
        recent = calls[max(0, end - window) : end]
        periodic = any(
            all(
                recent[-i] == recent[-i - period]
                for i in range(1, len(recent) - period + 1)
            )
            for period in range(1, min(max_period, len(recent) - 1) + 1)
        )
        low_variety = len(recent) >= window and len(set(recent)) <= distinct_threshold
        flagged = periodic or low_variety
        if flagged and not flagged_before:
            loops += 1
        flagged_before = flagged
    return loops


# The worker's file-size ceiling, and the ceiling on the Pi event transcript
# read back from it. This is deliberately not `limits.response_bytes`, which
# bounds one model response: the transcript records every streamed delta of
# every turn, so on a multi-file reading task it is an order of magnitude
# larger, and borrowing that limit killed the worker with SIGXFSZ mid-task.
# Half the container's 16 MiB /tmp, so the tmpfs itself is never the wall.
TRANSCRIPT_BYTES = 8 * 1048576


def readable_prefixes(worker_files):
    """Fixture files an agent's `read` tool may open, besides its own outputs.

    A flat file (no `/`) is readable at its exact fixture path; a nested file
    is readable through its top-level directory prefix, so a corpus of many
    files does not need to be listed one by one in the sandbox policy.
    """
    prefixes = set()
    for relpath in worker_files:
        if "/" in relpath:
            prefixes.add(f"/fixture/{relpath.split('/', 1)[0]}/")
        else:
            prefixes.add(f"/fixture/{relpath}")
    return sorted(prefixes)


def tree_digest(root):
    """Content digest of every regular file under root, for tamper detection.

    Independent of `core.digest`: this hashes an ordered stream of (path,
    content) pairs rather than a JSON-serializable value.
    """
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(len(content).to_bytes(8, "big"))
        h.update(content)
    return h.hexdigest()


# What the Docker client needs to reach the local engine; a Pi run takes no
# credential, and its container's environment is set with --env.
DOCKER_CLIENT_ENV = (
    "PATH",
    "HOME",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)


def bounded(command, timeout=10, limit=1_000_000):
    """Bound output in memory and kill only this process group on exit/failure."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=child_env(*DOCKER_CLIENT_ENV),
    )
    output = bytearray()
    end = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= end:
                    raise Failure("timeout", "timeout")
                for key, _ in selector.select(
                    min(0.1, max(0.001, end - time.monotonic()))
                ):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > limit:
                            raise Failure("completion_failure", "response_limit")
            return process.wait(timeout=max(0.01, end - time.monotonic())), bytes(
                output
            )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()


def snapshot(raw, disk_bytes, allowed):
    """No archive extraction onto the host; reject links, extra paths and large files."""
    result = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                if member.isdir() and member.name.rstrip("/") in ("work", "."):
                    continue
                name = member.name.removeprefix("work/").removeprefix("./")
                if (
                    name not in allowed
                    or not member.isfile()
                    or member.size > min(disk_bytes, 32768)
                    or name in result
                ):
                    raise Failure("task_quality_failure", "unauthorized_file")
                result[name] = archive.extractfile(member).read(32769)
    except (tarfile.TarError, OSError):
        raise Failure("task_quality_failure", "unauthorized_file") from None
    return result


def local_engine_preflight():
    """Reject remote Docker and hosts without enforced resource limits."""
    if os.environ.get("DOCKER_HOST", "unix://").split(":", 1)[0] != "unix":
        raise ValueError()
    rc, raw = bounded(["docker", "context", "inspect"])
    if rc or not json.loads(raw)[0]["Endpoints"]["docker"]["Host"].startswith(
        "unix://"
    ):
        raise ValueError()
    rc, raw = bounded(["docker", "info", "--format", "{{json .}}"])
    info = json.loads(raw)
    if rc or not all(
        info.get(key) is True
        for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
    ):
        raise ValueError()


class PiRunner:
    def __init__(self, c):
        self.c, self.owned, self.cleanup = c, [], "not_needed"
        self.journal = None

    def own(self, name):
        self.owned.append(name)
        if self.journal:
            atomic_json(self.journal, {"containers": self.owned, "cleanup": "pending"})

    def preflight(self):
        c = self.c
        if c["credential_env"] is not None:
            raise Failure("infrastructure_failure", "isolation_unavailable")
        try:
            local_engine_preflight()
            rc, raw = bounded(["docker", "image", "inspect", c["harness"]["image"]])
            if rc:
                raise ValueError()
            rc, raw = bounded(["docker", "network", "inspect", c["harness"]["network"]])
            networks = json.loads(raw)
            if rc or len(networks) != 1 or networks[0].get("Internal") is not True:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise Failure("infrastructure_failure", "isolation_unavailable") from None

    def flags(self, name, network):
        limits = self.c["limits"]
        return [
            "docker",
            "run",
            "--pull=never",
            "--name",
            name,
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            network,
            "--memory",
            f"{limits['worker_memory_mb']}m",
            "--memory-swap",
            f"{limits['worker_memory_mb']}m",
            "--cpus",
            str(limits["worker_cpus"]),
            "--pids-limit",
            str(limits["worker_pids"]),
            "--log-driver=none",
            "--tmpfs",
            f"/work:rw,nosuid,nodev,noexec,size={limits['worker_disk_mb']}m,mode=1777",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--tmpfs",
            "/home/pi:rw,nosuid,nodev,noexec,size=16m,mode=1777",
            "--env",
            "HOME=/home/pi",
            "--env",
            "PI_CODING_AGENT_DIR=/home/pi/.pi/agent",
            "--env",
            "PI_OFFLINE=1",
            "--env",
            "PI_TELEMETRY=0",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--workdir",
            "/work",
            "--entrypoint",
            "python3",
        ]

    def run(self, fixture):
        self.preflight()
        c, limits = self.c, self.c["limits"]
        started = time.monotonic()
        # Opt-in coding-workload variant (see (e) in the long-loop learnings):
        # force at least one compaction instead of disabling it, and check the
        # fixture's own report contract still validates afterward. Never
        # applies to investigation, which has no equivalent lab finding.
        gate = active_compaction_gate(c, fixture.name)
        gate_enabled = gate is not None
        with tempfile.TemporaryDirectory(prefix="toolkit-fixture-") as folder:
            root = Path(folder) / "fixture"
            root.mkdir(mode=0o755)
            for relpath, content in {
                **fixture.worker_files,
                **fixture.starter_outputs,
            }.items():
                path = root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            thinks = c["settings"]["reasoning"] != "off"
            off_effort = c["settings"].get("reasoning_off_effort")
            provider = {
                "providers": {
                    "toolkit": {
                        "baseUrl": c["harness"].get("endpoint") or c["endpoint"],
                        "api": "openai-completions",
                        "apiKey": "unused-local",
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": thinks or off_effort is not None,
                            "maxTokensField": "max_tokens",
                        },
                        "models": [
                            {
                                "id": c["model"]["id"],
                                "name": c["model"]["id"],
                                "reasoning": thinks or off_effort is not None,
                                **(
                                    {"thinkingLevelMap": {"off": off_effort}}
                                    if off_effort is not None and not thinks
                                    else {}
                                ),
                                "input": ["text"],
                                "samplingParams": {
                                    "temperature": c["settings"]["temperature"]
                                },
                                "contextWindow": gate["context_tokens"]
                                if gate_enabled
                                else c["settings"]["context_tokens"],
                                "maxTokens": c["settings"]["max_output_tokens"],
                            }
                        ],
                    }
                }
            }
            (root / "models.json").write_text(json.dumps(provider))
            # Disable retries/compaction and all inherited discovery by default.
            # The compaction gate flips this to force at least one compaction
            # instead. Timeout bounds the complete Pi process; the 12-turn cap
            # is enforced by an extension.
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "retry": {"enabled": False, "maxRetries": 0},
                        "compaction": {
                            "enabled": True,
                            "reserveTokens": gate["reserve_tokens"],
                            "keepRecentTokens": gate["keep_recent_tokens"],
                        }
                        if gate_enabled
                        else {"enabled": False},
                        "enableInstallTelemetry": False,
                    }
                )
            )
            allowed_write = json.dumps([f"/work/{n}" for n in fixture.allowed_outputs])
            readable = json.dumps(readable_prefixes(fixture.worker_files))
            verifier_command_json = json.dumps(fixture.verifier_command or "")
            # Only injected when the compaction gate is on: the report contract
            # re-asserted in the system prompt on every request, which
            # compaction never touches, instead of only in the first user
            # turn, which it can summarize away.
            report_contract_json = json.dumps(
                fixture.report_contract
                if gate_enabled and fixture.report_contract
                else ""
            )
            (root / "limit.ts").write_text(
                data("pi-policy.ts")
                .replace("__TEMPERATURE__", str(c["settings"]["temperature"]))
                .replace("__MAX_OUTPUT__", str(c["settings"]["max_output_tokens"]))
                .replace("__VERIFIER_COMMAND_JSON__", verifier_command_json)
                .replace("__ALLOWED_WRITE_JSON__", allowed_write)
                .replace("__READABLE_PREFIXES_JSON__", readable)
                .replace("__REPORT_CONTRACT_JSON__", report_contract_json)
            )
            for path in root.rglob("*"):
                path.chmod(0o555 if path.is_dir() else 0o444)
            fixture_hash = tree_digest(root)
            name = "toolkit-worker-" + uuid.uuid4().hex
            self.own(name)
            starter_copy = "".join(
                f'shutil.copyfile("/fixture/{n}","/work/{n}"); '
                for n in fixture.starter_outputs
            )
            launch = (
                "import os,shutil,subprocess,sys,resource,time,threading; "
                "threading.Timer(int(sys.argv[6])+20,lambda:os._exit(124)).start(); "
                'os.makedirs("/home/pi/.pi/agent",exist_ok=True); '
                '[shutil.copyfile("/fixture/"+n,"/home/pi/.pi/agent/"+n) for n in ["models.json","settings.json"]]; '
                + starter_copy
                + 'v=subprocess.check_output(["pi","--version"],text=True).strip(); '
                "sys.exit(42) if v != sys.argv[1] else None; "
                "resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[5]),int(sys.argv[5]))); "
                # No core dump: a signalled worker must not drop a `core` into /work,
                # where it would be read back as an unauthorized task output.
                "resource.setrlimit(resource.RLIMIT_CORE,(0,0)); "
                'log=open("/tmp/events","wb"); '
                'p=subprocess.run(["pi","--print","--mode","json","--no-session","--offline","--no-extensions","--extension","/fixture/limit.ts",'
                '"--no-skills","--no-prompt-templates","--no-themes","--no-context-files","--no-approve",'
                '"--provider","toolkit","--model",sys.argv[2],"--thinking",sys.argv[3],"--tools",sys.argv[7],"--",sys.argv[4]],stdout=log,stderr=log); '
                'log.close();open("/tmp/status","w").write(str(p.returncode));time.sleep(650)'
            )
            command = self.flags(name, c["harness"]["network"]) + [
                "--detach",
                "--volume",
                f"{root}:/fixture:ro",
                c["harness"]["image"],
                "-c",
                launch,
                c["harness"]["version"],
                c["model"]["id"],
                c["settings"]["reasoning"],
                fixture.prompt,
                str(TRANSCRIPT_BYTES),
                str(limits["request_seconds"]),
                fixture.tools,
            ]
            try:
                rc, _ = bounded(
                    command,
                    min(15, limits["request_seconds"]),
                    limits["response_bytes"],
                )
                if rc:
                    raise Failure("infrastructure_failure", "worker_failed")
                deadline = started + limits["request_seconds"]
                while True:
                    if time.monotonic() >= deadline:
                        raise Failure("timeout", "timeout")
                    rc, status = bounded(
                        [
                            "docker",
                            "exec",
                            name,
                            "python3",
                            "-c",
                            'from pathlib import Path;p=Path("/tmp/status");print(p.read_text() if p.exists() else "pending")',
                        ],
                        timeout=min(5, max(0.1, deadline - time.monotonic())),
                        limit=1024,
                    )
                    if rc:
                        raise Failure("completion_failure", "worker_failed")
                    if status.strip() != b"pending":
                        break
                    time.sleep(0.25)
                rc, events = bounded(
                    ["docker", "exec", name, "cat", "/tmp/events"],
                    limit=TRANSCRIPT_BYTES,
                )
                if rc or status.strip() != b"0":
                    raise Failure("completion_failure", "worker_failed")
                # docker cp cannot read tmpfs mounts, so stream the archive from inside.
                rc, raw = bounded(
                    ["docker", "exec", name, "tar", "-C", "/work", "-cf", "-", "."],
                    limit=limits["worker_disk_mb"] * 1048576,
                )
                if rc:
                    raise Failure("infrastructure_failure", "unavailable")
                outputs = snapshot(
                    raw,
                    limits["worker_disk_mb"] * 1048576,
                    allowed=fixture.allowed_outputs,
                )
                if tree_digest(root) != fixture_hash:
                    raise Failure("task_quality_failure", "fixture_changed")
                if fixture.candidate_output not in outputs:
                    raise Failure("task_quality_failure", "wrong_output")
                # A separate, never-worker-mounted directory: this is where the
                # investigation answer key (and any other verifier-only file)
                # lives, so the worker container never has access to it.
                verifier_root = Path(folder) / "verifier-fixture"
                verifier_root.mkdir(mode=0o755)
                for relpath, content in fixture.verifier_files.items():
                    path = verifier_root / relpath
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                (verifier_root / fixture.candidate_name).write_bytes(
                    outputs[fixture.candidate_output]
                )
                for path in verifier_root.rglob("*"):
                    path.chmod(0o555 if path.is_dir() else 0o444)
                verifier = "toolkit-verifier-" + uuid.uuid4().hex
                self.own(verifier)
                verify = self.flags(verifier, "none") + [
                    "--volume",
                    f"{verifier_root}:/fixture:ro",
                    c["harness"]["image"],
                    "-I",
                    *fixture.verify_argv(),
                ]
                rc, out = bounded(verify, min(15, limits["request_seconds"]), 16384)
                independent = (
                    rc == 0 and out.strip() == fixture.expected_verifier_stdout
                )
                complete = fixture.completion(
                    events, outputs.get(fixture.result_filename, b"")
                )
                metrics = empty_metrics()
                metrics["wall_seconds"] = time.monotonic() - started
                metrics["compactions"] = count_compactions(events)
                metrics["loops_detected"] = count_loops(events)
                return {
                    "metrics": metrics,
                    "response_sha256": digest(events),
                    "independent_pass": independent,
                    "completion_pass": complete,
                }
            finally:
                self.cleanup_owned()

    def cleanup_owned(self):
        failed = False
        for name in self.owned:
            try:
                rc, _ = bounded(["docker", "rm", "--force", name], timeout=10)
                failed |= rc != 0
            except (OSError, Failure):
                failed = True
        self.cleanup = "failed" if failed else "passed"
        if self.journal:
            atomic_json(
                self.journal, {"containers": self.owned, "cleanup": self.cleanup}
            )

"""Sample a local server process's memory during a run, and say only what that shows.

The operator names the server (`memory_probe.pid`, or `memory_probe.process` for an
exact executable name that must match exactly one process). Toolkit then reads, on
the machine it runs on, with no privileges and no downloads:

* Linux: the process's resident set (`VmRSS`) from `/proc/<pid>/status`, and, for
  each AMD GPU, the *device-wide* VRAM in use from
  `/sys/class/drm/card*/device/mem_info_vram_used`;
* macOS: the process's resident set from `ps`.

What that is, and is not:

* RSS is host memory the process has resident. Weights that live in GPU memory are
  not in it, and memory-mapped weights are (once touched).
* The AMD VRAM counter covers every process on the card, not this one; it is
  reported per card, never summed, and never attributed to the server.
* On Apple silicon the CPU and GPU share one pool. `ps` RSS does not establish the
  GPU-wired share, so unified memory stays unmeasured; nothing is added to system
  RAM.
* The peak is the highest of the samples taken (every attempt boundary, and every
  `INTERVAL_SECONDS` in between), so it is a lower bound on the true peak.
* A remote endpoint says nothing about this machine: the probe is refused for one.
  Through `target-run` the run, and so the sampling, happens on the target next to
  the server, and the PID is the target's.

Sampling is evidence, never a gate: any failure to read leaves the metric null.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

from .inspection import drm_cards, pci_device_id, sysfs_number, sysfs_read

INTERVAL_SECONDS = 0.5
MAX_SAMPLES = 20_000
NAME = re.compile(r"[A-Za-z0-9._+-]{1,64}")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
# Sampling is evidence, never a gate: an attribute that cannot be decoded reads as
# missing here, where the inventory lets the error through.
UNREADABLE = (OSError, ValueError)


class ProcessGone(Exception):
    """The named process exited, or its PID now belongs to another process."""


class Unreadable(Exception):
    """The process exists but its memory cannot be read on this platform."""


def clean_name(value):
    """A short executable name safe to store, or None."""
    return value if isinstance(value, str) and NAME.fullmatch(value) else None


def run_ps(args):
    """`ps` with a bounded wait; the empty string when it fails."""
    try:
        done = subprocess.run(
            ["ps", *args], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


class LinuxReader:
    source = "linux-procfs"

    def __init__(self, pid, proc_root=Path("/proc"), drm_root=Path("/sys/class/drm")):
        self.pid, self.proc, self.drm = pid, Path(proc_root), Path(drm_root)
        self.started = None

    def text(self, name, limit=65536):
        try:
            with (self.proc / str(self.pid) / name).open() as stream:
                return stream.read(limit)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            raise ProcessGone() from None
        except OSError:
            raise Unreadable() from None

    def start_time(self):
        # Field 22 of /proc/<pid>/stat; the command name (field 2) may hold spaces
        # and parentheses, so split after its closing one.
        try:
            return self.text("stat").rsplit(")", 1)[1].split()[19]
        except IndexError:
            raise Unreadable() from None

    def name(self):
        return clean_name(self.text("comm", 64).strip())

    def vram(self):
        """Device-wide VRAM in use on each AMD card that exposes it. Never summed."""
        pools = []
        for card in drm_cards(self.drm):
            device = card / "device"
            vendor = sysfs_read(device / "vendor", unreadable=UNREADABLE)
            if vendor is None:
                continue
            pci = sysfs_read(device / "device", unreadable=UNREADABLE)
            if pci is None:
                continue
            used = sysfs_number(device / "mem_info_vram_used", UNREADABLE)
            if vendor[:16].strip() != "0x1002" or used is None:
                continue
            pools.append(
                {
                    "drm_card": int(card.name[4:]),
                    "pci_device_id": pci_device_id(pci[:16].strip()),
                    "total_bytes": sysfs_number(
                        device / "mem_info_vram_total", UNREADABLE
                    ),
                    "used_bytes": used,
                }
            )
        return pools

    def read(self):
        status = self.text("status")
        if re.search(r"^State:\s+Z", status, re.MULTILINE):
            raise ProcessGone()
        found = re.search(r"^VmRSS:\s+([0-9]+)\s+kB", status, re.MULTILINE)
        if not found:
            raise Unreadable()
        start = self.start_time()
        if self.started is None:
            self.started = start
        elif start != self.started:
            raise ProcessGone()
        return {"rss_bytes": int(found.group(1)) * 1024, "vram": self.vram()}


class DarwinReader:
    source = "macos-ps"

    def __init__(self, pid, ps=run_ps):
        self.pid, self.ps = pid, ps
        self.started = None

    def name(self):
        comm = self.ps(["-o", "comm=", "-p", str(self.pid)])
        return clean_name(os.path.basename(comm)) if comm else None

    def read(self):
        row = self.ps(["-o", "rss=,lstart=", "-p", str(self.pid)]).strip()
        if not row:
            raise ProcessGone()
        rss, _, start = row.partition(" ")
        start = start.strip()
        if not rss.isdigit() or not start:
            raise Unreadable()
        if self.started is None:
            self.started = start
        elif start != self.started:
            raise ProcessGone()
        # `ps` reports KiB. No VRAM is read: on Apple silicon the GPU shares system
        # memory and RSS does not show it.
        return {"rss_bytes": int(rss) * 1024, "vram": []}


def find_pids(name, system, proc_root=Path("/proc"), ps=run_ps):
    """PIDs whose executable name is exactly `name`, excluding this process."""
    pids = []
    if system == "Linux":
        for entry in Path(proc_root).iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                comm = (entry / "comm").read_text(errors="replace").strip()
            except OSError:
                continue
            # The kernel truncates a command name to 15 characters.
            if comm == name[:15]:
                pids.append(int(entry.name))
    elif system == "Darwin":
        for line in ps(["-axo", "pid=,comm="]).splitlines():
            pid, _, comm = line.strip().partition(" ")
            if (
                pid.isdigit()
                and int(pid) != os.getpid()
                and os.path.basename(comm.strip()) == name
            ):
                pids.append(int(pid))
    return pids


def open_reader(
    spec,
    system=None,
    proc_root=Path("/proc"),
    drm_root=Path("/sys/class/drm"),
    ps=run_ps,
):
    """(reader, None) for a process that can be sampled, else (None, reason).

    The reasons are `unsupported_platform`, `process_not_found`,
    `process_ambiguous` (more than one process has that name: name the PID) and
    `unreadable`. A first read is taken here, so a PID that does not exist or
    cannot be read is refused before the run starts, not discovered partway.
    """
    system = system or platform.system()
    if system not in ("Linux", "Darwin"):
        return None, "unsupported_platform"
    pid = spec.get("pid")
    if pid is None:
        found = find_pids(spec["process"], system, proc_root, ps)
        if not found:
            return None, "process_not_found"
        if len(found) > 1:
            return None, "process_ambiguous"
        pid = found[0]
    reader = (
        LinuxReader(pid, proc_root, drm_root)
        if system == "Linux"
        else DarwinReader(pid, ps)
    )
    try:
        reader.read()
    except ProcessGone:
        return None, "process_not_found"
    except Unreadable:
        return None, "unreadable"
    return reader, None


class MemorySampler:
    """Background sampling plus a sample at every attempt boundary.

    `reader.read()` returns `{"rss_bytes", "vram": [pool, ...]}` or raises
    ProcessGone/Unreadable. Failures are recorded, never raised: this is evidence,
    not a gate.
    """

    def __init__(
        self, reader, name=None, interval=INTERVAL_SECONDS, clock=time.monotonic
    ):
        self.reader, self.name = reader, name
        self.interval, self.clock = interval, clock
        self.samples, self.exited = [], False
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = None

    def sample_now(self):
        if self.exited or len(self.samples) >= MAX_SAMPLES:
            return None
        try:
            sample = self.reader.read()
        except ProcessGone:
            self.exited = True
            return None
        except (Unreadable, OSError, ValueError):
            return None
        with self.lock:
            self.samples.append((self.clock(), sample))
        return sample

    def start(self):
        self.sample_now()

        def loop():
            while not self.stopping.wait(self.interval) and not self.exited:
                self.sample_now()

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def mark(self):
        """Start of a window: the time to pass to `peak_since`, after a sample."""
        started = self.clock()
        self.sample_now()
        return started

    def peak_since(self, started):
        """Highest resident set sampled since `started`, or None if none was."""
        self.sample_now()
        with self.lock:
            values = [s["rss_bytes"] for t, s in self.samples if t >= started]
        return max(values) if values else None

    def summary(self):
        with self.lock:
            samples = [s for _, s in self.samples]
        pools = {}
        for sample in samples:
            for pool in sample["vram"]:
                row = pools.setdefault(
                    pool["drm_card"],
                    {
                        "drm_card": pool["drm_card"],
                        "pci_device_id": pool["pci_device_id"],
                        "total_bytes": pool["total_bytes"],
                        "first_used_bytes": pool["used_bytes"],
                        "peak_used_bytes": pool["used_bytes"],
                        "scope": "device_wide",
                    },
                )
                row["peak_used_bytes"] = max(row["peak_used_bytes"], pool["used_bytes"])
        return {
            "status": "sampled" if samples else "unavailable",
            "reason": None if samples else "unreadable",
            "source": self.reader.source,
            "process_name": self.name,
            "interval_seconds": self.interval,
            "samples": len(samples),
            "process_exited": self.exited,
            "first_process_rss_bytes": samples[0]["rss_bytes"] if samples else None,
            "peak_process_rss_bytes": max(s["rss_bytes"] for s in samples)
            if samples
            else None,
            "gpu_pools": [pools[k] for k in sorted(pools)],
        }


def open_sampler(spec, reader=None, interval=INTERVAL_SECONDS):
    """(sampler, None) ready to start, or (None, the stored `unavailable` record).

    `reader` lets a caller supply an already-opened reader instead of resolving
    the named process on this machine.
    """
    reason = None
    if reader is None:
        reader, reason = open_reader(spec)
    if reader is None:
        return None, unavailable(reason)
    try:
        name = clean_name(reader.name())
    except (ProcessGone, Unreadable, OSError, ValueError):
        name = None
    return MemorySampler(reader, name, interval), None


def unavailable(reason):
    """The stored record when sampling could not start."""
    return {
        "status": "unavailable",
        "reason": reason,
        "source": None,
        "process_name": None,
        "interval_seconds": INTERVAL_SECONDS,
        "samples": 0,
        "process_exited": False,
        "first_process_rss_bytes": None,
        "peak_process_rss_bytes": None,
        "gpu_pools": [],
    }


def endpoint_is_local(endpoint):
    from urllib.parse import urlsplit

    return urlsplit(endpoint).hostname in LOOPBACK

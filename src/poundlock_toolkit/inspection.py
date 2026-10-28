"""Read-only best-effort local inventory; never inspect remote hardware by analogy."""

import contextlib
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from .core import child_env, now


CARD = re.compile(r"card[0-9]+")
PCI_ID = re.compile(r"0x[0-9a-fA-F]{4}")


def drm_cards(root, limit=None):
    """The `card<N>` entries of a DRM class directory, by name, leaving out
    connectors such as card0-DP-1. `limit` caps how many entries are looked at."""
    return [
        card
        for card in sorted(Path(root).glob("card*"))[:limit]
        if CARD.fullmatch(card.name)
    ]


def sysfs_read(path, limit=None, unreadable=(OSError,)):
    """Up to `limit` characters of a sysfs attribute (all of it for None), or
    None when reading raises one of `unreadable`."""
    try:
        with path.open() as stream:
            return stream.read(limit)
    except unreadable:
        return None


def sysfs_number(path, unreadable=(OSError,)):
    """An attribute holding a plain decimal count, such as VRAM bytes, or None."""
    text = (sysfs_read(path, 80, unreadable) or "").strip()
    return int(text) if text.isdigit() and len(text) < 21 else None


def pci_device_id(text):
    return text if PCI_ID.fullmatch(text) else None


def drm_inventory(root=Path("/sys/class/drm")):
    """Bounded Linux ABI reads. Missing Intel/AMD memory reporting remains unknown."""
    devices = []

    def value(path):
        return (sysfs_read(path, 80) or "").strip()

    for card in drm_cards(root, 64):
        device = card / "device"
        vendor = {"0x1002": "amd", "0x8086": "intel", "0x10de": "nvidia"}.get(
            value(device / "vendor"), "unknown"
        )
        pci = value(device / "device")
        driver = (
            (device / "driver").resolve().name
            if (device / "driver").exists()
            else "unknown"
        )
        if driver not in ("amdgpu", "i915", "xe", "nvidia", "nouveau"):
            driver = "unknown"
        amd = vendor == "amd"
        total = sysfs_number(device / "mem_info_vram_total") if amd else None
        used = sysfs_number(device / "mem_info_vram_used") if amd else None
        devices.append(
            {
                "vendor": vendor,
                "pci_device_id": pci_device_id(pci),
                "driver": driver,
                "total_bytes": total,
                "available_bytes": total - used
                if total is not None and used is not None and 0 <= used <= total
                else None,
                "source": "linux-drm-sysfs",
            }
        )
    return devices


def command(args):
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=child_env("PATH", "LANG", "LC_ALL"),
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def inspect():
    system = platform.system()
    devices = []
    memory: dict[str, Any] = {
        "system_ram_bytes": None,
        "system_available_bytes": None,
        "unified_memory_bytes": None,
        "gpu_pools": [],
        "placement": "unknown",
    }
    if system == "Linux":
        devices = drm_inventory()
        try:
            fields = dict(
                line.split(":", 1)
                for line in Path("/proc/meminfo").read_text().splitlines()
            )
            memory["system_ram_bytes"] = int(fields["MemTotal"].split()[0]) * 1024
            memory["system_available_bytes"] = (
                int(fields["MemAvailable"].split()[0]) * 1024
            )
        except (OSError, ValueError, KeyError):
            pass
        if shutil.which("nvidia-smi"):
            output = command(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ]
            )
            with contextlib.suppress(ValueError):
                memory["gpu_pools"] = [
                    {
                        "total_bytes": int(a) * 1048576,
                        "available_bytes": int(b) * 1048576,
                    }
                    for a, b in (line.split(",") for line in output.splitlines())
                ]
        memory["gpu_pools"].extend(
            {"total_bytes": d["total_bytes"], "available_bytes": d["available_bytes"]}
            for d in devices
            if d["vendor"] in ("amd", "intel")
        )
    elif system == "Darwin":
        value = command(["sysctl", "-n", "hw.memsize"])
        if value.isdigit():
            memory["system_ram_bytes"] = int(value)
            if platform.machine() == "arm64":
                memory["unified_memory_bytes"] = int(
                    value
                )  # Same physical pool; never sum.
    return {
        "observed_at": now(),
        "os": system if system in ("Linux", "Darwin", "Windows") else "other",
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory": memory,
        "gpu_devices": devices,
        "software_present": {
            x: bool(shutil.which(x))
            for x in ("python3", "docker", "pi", "ollama", "llama-server", "omlx")
        },
        "unknowns": [
            "Remote inventory is unknown unless explicitly supplied.",
            "Executable presence does not establish working hardware support.",
            "GPU pools are independent; no cross-device allocation is assumed.",
        ],
    }

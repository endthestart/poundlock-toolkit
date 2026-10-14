"""Read-only best-effort local inventory; never inspect remote hardware by analogy."""

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from .core import now


def drm_inventory(root=Path("/sys/class/drm")):
    """Bounded Linux ABI reads. Missing Intel/AMD memory reporting remains unknown."""
    devices = []

    def value(path):
        try:
            with path.open() as f:
                return f.read(80).strip()
        except OSError:
            return ""

    def number(path):
        v = value(path)
        return int(v) if v.isdigit() and len(v) < 21 else None

    for card in sorted(root.glob("card*"))[:64]:
        if not re.fullmatch(r"card[0-9]+", card.name):
            continue
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
        total = number(device / "mem_info_vram_total") if vendor == "amd" else None
        used = number(device / "mem_info_vram_used") if vendor == "amd" else None
        devices.append(
            {
                "vendor": vendor,
                "pci_device_id": pci
                if re.fullmatch(r"0x[0-9a-fA-F]{4}", pci)
                else None,
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
        p = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def inspect():
    system = platform.system()
    devices = []
    memory = {
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
            try:
                memory["gpu_pools"] = [
                    {
                        "total_bytes": int(a) * 1048576,
                        "available_bytes": int(b) * 1048576,
                    }
                    for a, b in (line.split(",") for line in output.splitlines())
                ]
            except ValueError:
                pass
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

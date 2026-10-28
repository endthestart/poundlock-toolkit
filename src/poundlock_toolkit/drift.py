"""Attach-time checks: name the reviewed assumption that no longer holds.

Stable, decision-relevant inventory (platform, CPU count, total memory, GPU
devices and pool sizes, relevant executables) must match the reviewed plan.
Changing capacity (free memory, timestamps) never invalidates that identity; it is
checked separately against the configuration's explicit requirement only.

Every check reports one status: pass, fail, unknown or stale.
"""

from __future__ import annotations

import re
from typing import Any

from .core import digest

STATUSES = ("pass", "fail", "unknown", "stale")

# Sections of the reviewed configuration, named when their content drifts. Endpoint
# URLs and credential variable names are deliberately not digested individually.
INPUT_SECTIONS = {
    "model": lambda c: c["model"],
    "backend_build": lambda c: {
        k: c["backend"].get(k) for k in ("name", "version", "commit")
    },
    "serving_application": lambda c: c["backend"].get("application", "unrecorded"),
    "settings": lambda c: c["settings"],
    "harness": lambda c: {k: v for k, v in c["harness"].items() if k != "endpoint"},
    "inventory": lambda c: c["inventory"],
    "workloads": lambda c: c["workloads"],
    "limits": lambda c: c["limits"],
    "requirements": lambda c: c["requirements"],
    "destination": lambda c: [c["data_destination"], c["paid"], c["api"]],
    "tool_schema": lambda c: {
        k: v for k, v in c.get("tool_schema", {}).items() if k != "endpoints"
    },
    "investigation": lambda c: c.get("investigation", {}),
}

# Present in the reviewed inputs only when the configuration carries them, so a plan
# reviewed before the feature existed still matches an unchanged configuration.
OPTIONAL_SECTIONS = {
    "memory_probe": lambda c: c.get("memory_probe"),
}

NEXT = {
    "implementation": "Install the exact package the plan was reviewed with, or re-run target-plan with this package and review it.",
    "target": "Use the target the plan was reviewed for, or re-run target-plan for this target.",
    "plan_version": "The reviewed plan predates this package's plan format; re-run plan/target-plan and review it.",
    "reviewed_inputs": "Configuration changed since review; re-run plan/target-plan and review the named sections.",
    "stable_inventory": "Target inventory changed since review; re-run target-plan and review the named assumptions.",
    "capacity": "Free memory is below the configured requirement; free capacity or change the requirement and re-plan, then retry.",
}


class DriftError(ValueError):
    """A reviewed assumption drifted or an attach-time check failed."""

    def __init__(self, checks):
        super().__init__("Attach checks failed")
        self.checks = checks

    def report(self):
        return check_report(self.checks)


def reviewed_inputs(c):
    sections = {name: digest(view(c)) for name, view in INPUT_SECTIONS.items()}
    for name, view in OPTIONAL_SECTIONS.items():
        if view(c) is not None:
            sections[name] = digest(view(c))
    return sections


def check(name, status, **detail):
    assert status in STATUSES
    return dict(check=name, status=status, **detail)


def evaluation_check(reviewed, current):
    """Compare a reviewed evaluation plan with one computed from current inputs.

    Names the configuration sections that drifted. Endpoint-only drift appears as
    a configuration digest change without a named section.
    """
    if not isinstance(reviewed, dict) or not isinstance(
        reviewed.get("reviewed_inputs"), dict
    ):
        return check("plan_version", "stale")
    if reviewed.get("readiness_only") != current["readiness_only"]:
        raise ValueError("Readiness-only mode must match the reviewed plan")
    before, after = reviewed["reviewed_inputs"], current["reviewed_inputs"]
    drifted = sorted(
        k for k in set(before) | set(after) if before.get(k) != after.get(k)
    )
    if not drifted and reviewed.get("config_sha256") != current["config_sha256"]:
        drifted = ["endpoint_or_credential_reference"]
    if drifted:
        return check("reviewed_inputs", "stale", drifted=drifted)
    if reviewed != current:
        return check("plan_version", "stale")
    return check("reviewed_inputs", "pass")


def stable_inventory(inv):
    """Flat, decision-relevant view. Free memory and timestamps are excluded."""
    memory = inv.get("memory") or {}
    flat = {
        "os": inv.get("os"),
        "architecture": inv.get("architecture"),
        "cpu_count": inv.get("cpu_count"),
        "memory.system_ram_bytes": memory.get("system_ram_bytes"),
        "memory.unified_memory_bytes": memory.get("unified_memory_bytes"),
        "memory.gpu_pool_count": len(memory.get("gpu_pools") or []),
        "gpu_device_count": len(inv.get("gpu_devices") or []),
    }
    for n, pool in enumerate(memory.get("gpu_pools") or []):
        flat[f"memory.gpu_pools[{n}].total_bytes"] = pool.get("total_bytes")
    for n, device in enumerate(inv.get("gpu_devices") or []):
        for key in ("vendor", "pci_device_id", "driver", "total_bytes"):
            flat[f"gpu_devices[{n}].{key}"] = device.get(key)
    for name, present in sorted((inv.get("software_present") or {}).items()):
        flat[f"software_present.{name}"] = present
    return flat


def inventory_check(reviewed_inventory, observed_inventory):
    before = stable_inventory(reviewed_inventory)
    after = stable_inventory(observed_inventory)
    drifted: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        row = {"assumption": key, "reviewed": a, "observed": b}
        (unknown if b is None and a is not None else drifted).append(row)
    if drifted:
        return check("stable_inventory", "stale", drifted=drifted + unknown)
    if unknown:
        return check("stable_inventory", "unknown", drifted=unknown)
    return check("stable_inventory", "pass")


def capacity_check(c, observed_inventory):
    required = c["requirements"]["min_available_memory_bytes"]
    available = (observed_inventory.get("memory") or {}).get("system_available_bytes")
    if required is None:
        return check(
            "capacity", "pass", detail="No free-memory requirement is configured."
        )
    if available is None:
        return check("capacity", "unknown", required_bytes=required)
    return check(
        "capacity",
        "pass" if available >= required else "fail",
        required_bytes=required,
        available_bytes=available,
    )


def check_report(checks):
    failing = [c for c in checks if c["status"] != "pass"]
    return {
        "state": "attach_checks_failed" if failing else "attach_checks_passed",
        "checks": checks,
        "next_required_check": failing[0]["check"] if failing else None,
        "next": NEXT.get(failing[0]["check"], "Re-plan and review.")
        if failing
        else None,
    }


def require(checks):
    if any(c["status"] != "pass" for c in checks):
        raise DriftError(checks)
    return checks


_NAME = re.compile(r"[A-Za-z0-9_.\[\]-]{1,120}")


def sanitize(checks):
    """Bound and allowlist target-supplied checks before presenting them."""
    if not isinstance(checks, list) or len(checks) > 16:
        raise ValueError("Malformed attach checks")
    out = []
    for row in checks:
        if (
            not isinstance(row, dict)
            or row.get("check") not in NEXT
            or row.get("status") not in STATUSES
        ):
            raise ValueError("Malformed attach checks")
        clean = {"check": row["check"], "status": row["status"]}
        for key in ("required_bytes", "available_bytes"):
            if type(row.get(key)) is int:
                clean[key] = row[key]
        drifted = row.get("drifted", [])
        if not isinstance(drifted, list) or len(drifted) > 256:
            raise ValueError("Malformed attach checks")
        items: list[str | dict[str, Any]] = []
        for item in drifted:
            if isinstance(item, str) and _NAME.fullmatch(item):
                items.append(item)
            elif isinstance(item, dict) and _NAME.fullmatch(
                str(item.get("assumption", ""))
            ):
                items.append(
                    {
                        "assumption": item["assumption"],
                        **{
                            k: item.get(k)
                            if item.get(k) is None
                            or type(item.get(k)) in (bool, int)
                            or (
                                isinstance(item.get(k), str)
                                and _NAME.fullmatch(item[k])
                            )
                            else "unrepresentable"
                            for k in ("reviewed", "observed")
                        },
                    }
                )
            else:
                raise ValueError("Malformed attach checks")
        if items:
            clean["drifted"] = items
        out.append(clean)
    return out

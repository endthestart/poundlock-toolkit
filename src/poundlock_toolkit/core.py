"""Strict configuration, private storage and portable primitives."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import tempfile
from datetime import datetime, UTC
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    return hashlib.sha256(raw).hexdigest()


def data(name):
    return files("poundlock_toolkit").joinpath("data", name).read_text()


def data_tree(prefix):
    """Read a nested packaged data directory as {relative_posix_path: text}."""
    root = files("poundlock_toolkit").joinpath("data", *prefix.split("/"))
    result = {}

    def walk(node, rel):
        for child in sorted(node.iterdir(), key=lambda n: n.name):
            child_rel = f"{rel}/{child.name}" if rel else child.name
            if child.is_dir():
                walk(child, child_rel)
            else:
                result[child_rel] = child.read_text(encoding="utf-8")

    walk(root, "")
    return result


def validate(value, name="qualification.schema.json"):
    if (
        name == "qualification.schema.json"
        and isinstance(value, dict)
        and value.get("schema") == "urn:poundlock-toolkit:tool-schema:1"
    ):
        name = "tool-schema-result.schema.json"
    # Validation errors may contain secrets. Never propagate their rendering.
    # JSON Schema's numeric checks alone may accept Python float('nan').
    json.dumps(value, allow_nan=False)
    validator = Draft202012Validator(
        json.loads(data(name)), format_checker=FormatChecker()
    )
    if not validator.is_valid(value):
        raise ValueError("Document does not conform to " + name)
    return value


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Document exceeds size limit")
    return json.loads(
        raw,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")),
    )


def atomic_json(path, value):
    """Adapted from an earlier research harness's atomic_json, with private permissions and fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".toolkit-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_run_path(output):
    """`output` as a Path, refusing a run that is already there."""
    output = Path(output)
    if output.exists():
        raise ValueError("Refusing to overwrite an existing run")
    return output


def numeric(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def config(path):
    value = validate(read_json(path), "config.schema.json")
    from urllib.parse import urlsplit

    endpoints = [value["endpoint"], value["harness"].get("endpoint")]
    endpoints.extend(value.get("tool_schema", {}).get("endpoints", {}).values())
    for endpoint in endpoints:
        if endpoint is None:
            continue
        url = urlsplit(endpoint)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Endpoint must be HTTP(S), without credentials, query or fragment"
            )
    if value["settings"]["max_output_tokens"] >= value["settings"]["context_tokens"]:
        raise ValueError("Output budget must leave input context")
    return value


def identity(c):
    # Endpoint and credential references deliberately excluded from export identity.
    return {
        k: c[k]
        for k in (
            "model",
            "backend",
            "settings",
            "harness",
            "inventory",
            "data_destination",
            "paid",
        )
    }


class Failure(Exception):
    def __init__(self, outcome, code):
        super().__init__(code)
        self.outcome, self.code = outcome, code


def bounded_signals():
    """Install a run's signal handlers; return the function that removes them.

    Until then SIGALRM, the per-request interval timer, raises a `timeout`
    Failure, and SIGTERM raises KeyboardInterrupt, so it cancels like Ctrl-C.
    Removing clears the timer and puts the previous handlers back.
    """

    def alarm(*_):
        raise Failure("timeout", "timeout")

    def terminate(*_):
        raise KeyboardInterrupt()

    old_alarm = signal.signal(signal.SIGALRM, alarm)
    old_term = signal.signal(signal.SIGTERM, terminate)

    def restore():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)

    return restore

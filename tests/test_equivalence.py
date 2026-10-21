"""Golden outputs that the measurement-path merges must not change.

Each file under tests/data/equivalence/ holds what the code produced before one of
the dead-code review's merges (M2, M3, M4, M8, M10, L5), on the synthetic demo and
on the responses recorded in tests/data/recorded-responses.json: the SHA-256 of
every version of each result document as written, each attempt's verdict, the
requests sent, and the reports printed. These tests produce them again and require
the same bytes.

Clocks, UUIDs and the network are replaced by deterministic stand-ins, so the
documents are reproducible byte for byte. A difference means a change altered
what Toolkit sends, measures, scores or writes; regenerate a golden file only for
a change that is meant to do that, and say so in its commit.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import poundlock_toolkit.core as core
import poundlock_toolkit.reporting as reporting
from poundlock_toolkit.backends import Backend, empty_metrics
from poundlock_toolkit.cli import main
from poundlock_toolkit.core import Failure, config
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.evaluation import evaluate
from poundlock_toolkit.fixtures import (
    CODING_FIXTURE,
    INVESTIGATION_FIXTURE,
    VERIFIER_COMMAND,
)
from poundlock_toolkit.inspection import drm_inventory
from poundlock_toolkit.memory_probe import LinuxReader
from poundlock_toolkit.schema_evaluation import evaluate as schema_evaluate
from poundlock_toolkit.schema_evaluation import request as schema_request

TESTS = Path(__file__).resolve().parent
EXAMPLES = TESTS.parent / "src/poundlock_toolkit/data/examples"
RECORDED = json.loads((TESTS / "data/recorded-responses.json").read_text())
GOLDEN = TESTS / "data/equivalence"
DEMOS = ("openai.json", "ollama.json", "pi.json", "investigation.json")
# Generous limits, so only a scenario that asks for an exhausted budget gets one.
LIMITS = {"limits.total_seconds": 600, "limits.request_seconds": 120}
# Each monotonic-clock read advances it by this much; each wall-clock read by a
# second from START. Reports are written as of REPORTED_AT.
STEP = 0.125
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
REPORTED_AT = datetime(2026, 1, 2, tzinfo=timezone.utc)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, indent=1, sort_keys=True) + "\n"


def body_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


@contextlib.contextmanager
def deterministic():
    """Sequential UUIDs and stepped clocks for everything run inside."""
    reads = {"uuid": 0, "monotonic": 0, "wall": 0}

    def uuid4():
        reads["uuid"] += 1
        return uuid.UUID(int=reads["uuid"])

    def monotonic():
        reads["monotonic"] += 1
        return reads["monotonic"] * STEP

    class Wall(datetime):
        @classmethod
        def now(cls, tz=None):
            reads["wall"] += 1
            return START + timedelta(seconds=reads["wall"])

    class Reported(datetime):
        @classmethod
        def now(cls, tz=None):
            return REPORTED_AT

    with (
        patch.object(uuid, "uuid4", uuid4),
        patch.object(time, "monotonic", monotonic),
        patch.object(core, "datetime", Wall),
        patch.object(reporting, "datetime", Reported),
    ):
        yield


class Replay:
    """The network: each request gets the next recorded response, in order."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        body = None if request.data is None else json.loads(request.data)
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(sorted(request.header_items())),
                "timeout": timeout,
                "body_sha256": None if body is None else body_digest(body),
                "messages_sha256": None
                if body is None
                else body_digest(body.get("messages")),
                "tools_sha256": None
                if body is None
                else body_digest(body.get("tools")),
                "fields": None
                if body is None
                else {k: v for k, v in body.items() if k not in ("messages", "tools")},
            }
        )
        if not self.responses:
            raise AssertionError("No recorded response left for " + request.full_url)
        response = self.responses.pop(0)
        if urlsplit(request.full_url).path != response["path"]:
            raise AssertionError(f"Expected {response['path']}: {request.full_url}")
        if "signal" in response:
            signal.raise_signal(getattr(signal, response["signal"]))
            raise AssertionError("The signal did not interrupt the request")
        if response.get("error") == "unreachable":
            raise urllib.error.URLError("recorded: unreachable")
        if response["status"] != 200:
            raise urllib.error.HTTPError(
                request.full_url, response["status"], "recorded", {}, io.BytesIO()
            )
        return io.BytesIO(response["body"].encode())


@contextlib.contextmanager
def recorded(replay, snapshots):
    """Serve requests from `replay`; note the digest of every persisted file."""
    replace = os.replace

    def open_(opener, request, data=None, timeout=None):
        return replay.open(request, timeout)

    def record(source, target):
        snapshots.append(sha256(Path(source).read_bytes()))
        return replace(source, target)

    with (
        patch.object(urllib.request.OpenerDirector, "open", open_),
        patch.object(os, "replace", record),
    ):
        yield


class RecordedPi:
    """PiRunner's place, with recorded attempt results and no containers."""

    cleanup = "passed"
    journal = None

    def __init__(self, results):
        self.results = list(results)

    def __call__(self, c):
        return self

    def run(self, fixture):
        result = copy.deepcopy(self.results.pop(0))
        result["metrics"] = dict(empty_metrics(), **result["metrics"])
        return result


def configured(example, settings, folder):
    """The named example configuration with dotted-path `settings` applied."""
    value = json.loads((EXAMPLES / example).read_text())
    for dotted, setting in settings.items():
        *parents, last = dotted.split(".")
        node = value
        for name in parents:
            node = node[name]
        node[last] = copy.deepcopy(setting)
    path = Path(folder) / "config.json"
    path.write_text(json.dumps(value))
    return config(path)


def rows(document):
    """Each attempt's place and verdict, so a golden file says what it covers."""
    return [
        [
            row.get("stage", "trial" if "repetition" in row else "warmup"),
            row.get("workload", row.get("boundary")),
            row.get("repetition"),
            row["outcome"],
            row["code"],
        ]
        for row in document.get("warmup", []) + document["attempts"]
    ]


def stored(output, snapshots):
    """The document as written: its bytes' digest and every earlier version's."""
    raw = output.read_bytes() if output.exists() else None
    return {
        "document_sha256": None if raw is None else sha256(raw),
        "persisted_sha256": snapshots,
        "rows": None if raw is None else rows(json.loads(raw)),
    }


def run(c, folder, replay, evaluator=evaluate, **kwargs):
    """One evaluation: the stored document, its history and what was sent."""
    output = Path(folder) / "result.json"
    progress, snapshots = [], []
    with deterministic(), recorded(replay, snapshots):
        try:
            returned = evaluator(
                c, output, progress=lambda *args: progress.append(list(args)), **kwargs
            )
            error = None
        except Exception as exc:  # recorded, so a changed failure shows up
            returned, error = None, type(exc).__name__
    result = stored(output, snapshots)
    result.update(
        returned_matches_file=returned is not None
        and returned == json.loads(output.read_bytes()),
        error=error,
        progress=progress,
        requests=replay.requests,
        unused_responses=len(replay.responses),
    )
    return result


def tool_schema_runs():
    result = {}
    for name, scenario in sorted(RECORDED["tool_schema"].items()):
        with tempfile.TemporaryDirectory() as folder:
            c = configured("tool-schema.json", LIMITS | scenario["config"], folder)
            result[name] = run(c, folder, Replay(scenario["responses"]))
    return result


def qualification_runs():
    result = {}
    for name, scenario in sorted(RECORDED["qualification"].items()):
        with tempfile.TemporaryDirectory() as folder:
            settings = LIMITS | {"limits.repetitions": 2} | scenario["config"]
            c = configured(scenario["example"], settings, folder)
            result[name] = run(
                c,
                folder,
                Replay(scenario["responses"]),
                pi_factory=RecordedPi(scenario.get("pi", ())),
            )
    for example in DEMOS:
        with tempfile.TemporaryDirectory() as folder:
            result["demo " + example] = demo(example, folder)
    return result


@contextlib.contextmanager
def without_interval_timer():
    """A platform without signal.setitimer."""
    setitimer = signal.setitimer
    del signal.setitimer
    try:
        yield
    finally:
        signal.setitimer = setitimer


def refusal(example, settings, *, existing=False, timer=True, replay=None, **kwargs):
    """What evaluate does with a run it must refuse, and what it leaves behind."""
    with tempfile.TemporaryDirectory() as folder:
        c = configured(example, LIMITS | settings, folder)
        output = Path(folder) / "result.json"
        if existing:
            output.write_text("an earlier run\n")
        snapshots = []
        with (
            deterministic(),
            recorded(replay or Replay(), snapshots),
            contextlib.nullcontext() if timer else without_interval_timer(),
        ):
            try:
                evaluate(c, output, **kwargs)
                outcome = "ran"
            except ValueError as exc:
                outcome = f"ValueError: {exc}"
        return {
            "outcome": outcome,
            "output_sha256": sha256(output.read_bytes()) if output.exists() else None,
            "persisted": len(snapshots),
        }


def installed():
    """What the run's SIGALRM and SIGTERM handlers do, and whether a timer is set."""
    seen = {}
    for name in ("SIGALRM", "SIGTERM"):
        number = getattr(signal, name)
        try:
            signal.getsignal(number)(number, None)
            seen[name] = "returned"
        except Failure as failure:
            seen[name] = f"Failure {failure.outcome}/{failure.code}"
        except KeyboardInterrupt:
            seen[name] = "KeyboardInterrupt"
    seen["timer_set"] = 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 120
    return seen


def around(evaluation):
    """Run with sentinel handlers installed; report whether the run restored them."""

    def alarm(*_):
        pass

    def term(*_):
        pass

    previous = signal.signal(signal.SIGALRM, alarm), signal.signal(signal.SIGTERM, term)
    try:
        result = evaluation()
        result["restored"] = {
            "SIGALRM": signal.getsignal(signal.SIGALRM) is alarm,
            "SIGTERM": signal.getsignal(signal.SIGTERM) is term,
            "timer_cleared": signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0),
        }
    finally:
        signal.signal(signal.SIGALRM, previous[0])
        signal.signal(signal.SIGTERM, previous[1])
    return result


class Observing(SyntheticBackend):
    """The demo backend, noting the signal handlers in force at each request."""

    def __init__(self, seen, fail=None):
        self.seen, self.fail = seen, fail

    def readiness(self):
        self.seen.append(installed())
        if self.fail:
            raise self.fail()
        return super().readiness()

    def complete(self, prompt, tool=False, stream=False):
        self.seen.append(installed())
        return super().complete(prompt, tool=tool, stream=stream)


class Sampled:
    """A memory reader with scripted samples and one AMD pool."""

    source = "linux-procfs"

    def __init__(self):
        self.reads = 0

    def name(self):
        return "llama-server"

    def read(self):
        self.reads += 1
        pool = {
            "drm_card": 0,
            "pci_device_id": "0x744c",
            "total_bytes": 17163091968,
            "used_bytes": 1073741824 + self.reads * 4096,
        }
        return {"rss_bytes": 2**30 + self.reads * 2**20, "vram": [pool]}


def schema_requester(seen, fail=None):
    responses = RECORDED["tool_schema"]["valid"]["responses"]
    arguments = json.loads(
        json.loads(responses[1]["body"])["choices"][0]["message"]["tool_calls"][0][
            "function"
        ]["arguments"]
    )

    def requester(c, boundary):
        seen.append(installed())
        if fail:
            raise fail()
        call = {
            "type": "function",
            "function": {"name": "propose_plan", "arguments": arguments},
        }
        return [call], True, None

    return requester


def guards():
    """The refusals and the bounded-signal set-up both evaluators share (M3)."""
    result = {}
    for example in ("openai.json", "tool-schema.json"):
        for case, settings, options in (
            ("plan blocked", {"paid": True}, {}),
            ("existing output", {}, {"existing": True}),
            ("no interval timer", {}, {"timer": False}),
            ("plan blocked, existing output", {"paid": True}, {"existing": True}),
            (
                "no interval timer, existing output",
                {},
                {"existing": True, "timer": False},
            ),
            ("plan blocked, no interval timer", {"paid": True}, {"timer": False}),
        ):
            result[f"{example} {case}"] = refusal(example, settings, **options)
    # Readiness only sends no inference, so a paid endpoint does not block it.
    models = RECORDED["qualification"]["openai-pass"]["responses"][:1]
    result["openai.json readiness only, paid"] = refusal(
        "openai.json", {"paid": True}, replay=Replay(models), readiness_only=True
    )
    result["tool-schema.json demo backend"] = refusal(
        "tool-schema.json", {}, synthetic=True
    )
    for name, fail in (("completes", None), ("raises", ZeroDivisionError)):
        seen = []

        def qualification():
            with tempfile.TemporaryDirectory() as folder:
                c = configured("openai.json", LIMITS, folder)
                return run(c, folder, Replay(), backend=Observing(seen, fail))

        result[f"openai.json handlers, run {name}"] = around(qualification)
        result[f"openai.json handlers, run {name}"]["seen"] = seen
        seen = []

        def tool_schema():
            with tempfile.TemporaryDirectory() as folder:
                c = configured("tool-schema.json", LIMITS, folder)
                requester = schema_requester(seen, fail)
                return run(c, folder, Replay(), schema_evaluate, requester=requester)

        result[f"tool-schema.json handlers, run {name}"] = around(tool_schema)
        result[f"tool-schema.json handlers, run {name}"]["seen"] = seen

    def memory():
        with tempfile.TemporaryDirectory() as folder:
            scenario = RECORDED["qualification"]["openai-pass"]
            c = configured("openai.json", LIMITS | {"limits.repetitions": 2}, folder)
            c["memory_probe"] = {"pid": 4242}
            return run(
                c,
                folder,
                Replay(scenario["responses"]),
                memory_reader=Sampled(),
                memory_interval=3600,
            )

    result["openai.json memory probe"] = around(memory)
    return result


class WireReplay(Replay):
    """Replay that also keeps the digest of each request body's exact bytes."""

    def open(self, request, timeout):
        response = super().open(request, timeout)
        data = request.data
        self.requests[-1]["raw_sha256"] = None if data is None else sha256(data)
        return response


def answered(call):
    """A transport call's result, or the failure it raised."""
    try:
        return {"result": call()}
    except Failure as failure:
        return {"failure": [failure.outcome, failure.code]}
    except Exception as exc:  # recorded, so a changed failure shows up
        return {"error": type(exc).__name__}


SETTINGS = {
    "off": {},
    "off, effort none": {
        "settings.reasoning_off_effort": "none",
        "settings.temperature": 0.7,
    },
    "low": {"settings.reasoning": "low", "settings.max_output_tokens": 64},
    "high": {"settings.reasoning": "high", "settings.temperature": 2},
}
HISTORY = (
    {"role": "user", "content": "Earlier question"},
    {"role": "assistant", "content": "Earlier answer"},
)


def openai_reply(content, finish="stop", calls=None):
    message = {"content": content}
    if calls is not None:
        message["tool_calls"] = calls
    body = {
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        "timings": {"predicted_per_second": 33.25, "prompt_ms": 12.5},
    }
    return {"path": "/v1/chat/completions", "status": 200, "body": json.dumps(body)}


def openai_stream(content, finish="stop"):
    events = [
        {"choices": [{"delta": {"content": content}}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}},
    ]
    text = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    return {
        "path": "/v1/chat/completions",
        "status": 200,
        "body": text + "data: [DONE]\n\n",
    }


def ollama_reply(content, reason="stop", calls=None, **counts):
    message = {"content": content}
    if calls is not None:
        message["tool_calls"] = calls
    body = {"message": message, "done": True, "done_reason": reason, **counts}
    return {"path": "/api/chat", "status": 200, "body": json.dumps(body)}


RECORD_CALL = [
    {
        "type": "function",
        "function": {
            "name": "record_value",
            "arguments": '{"key":"answer","value":123}',
        },
    }
]
COUNTS = {
    "prompt_eval_count": 40,
    "prompt_eval_duration": 80000000,
    "eval_count": 12,
    "eval_duration": 300000000,
}


def backend_calls(api):
    """Every way Toolkit asks an attached server for a completion, with replies."""
    if api == "openai":
        return [
            ("readiness", lambda b: b.readiness(), [RECORDED_MODELS]),
            ("complete", lambda b: b.complete("Say OK"), [openai_reply("OK")]),
            (
                "complete, tool",
                lambda b: b.complete("Call it", tool=True),
                [openai_reply(None, "tool_calls", RECORD_CALL)],
            ),
            (
                "complete, stream",
                lambda b: b.complete("Say OK", stream=True),
                [openai_stream("OK")],
            ),
            (
                "complete, tool and stream",
                lambda b: b.complete("Call it", tool=True, stream=True),
                [openai_stream("")],
            ),
            (
                "probe",
                lambda b: b.probe("Probe prompt", HISTORY),
                [openai_stream("O", "length")],
            ),
            (
                "complete, short and truncated",
                lambda b: b.complete(
                    "Say OK", max_tokens=3, truncation_ok=True, history=HISTORY
                ),
                [openai_reply("O", "length")],
            ),
        ]
    return [
        ("readiness", lambda b: b.readiness(), [RECORDED_TAGS]),
        ("complete", lambda b: b.complete("Say OK"), [ollama_reply("OK", **COUNTS)]),
        (
            "complete, tool",
            lambda b: b.complete("Call it", tool=True),
            [ollama_reply("", calls=RECORD_CALL)],
        ),
        (
            "complete, stream",
            lambda b: b.complete("Say OK", stream=True),
            [ollama_reply("OK", **COUNTS)],
        ),
        (
            "probe",
            lambda b: b.probe("Probe prompt", HISTORY),
            [ollama_reply("O", "length", **COUNTS)],
        ),
        (
            "complete, short and truncated",
            lambda b: b.complete(
                "Say OK", max_tokens=3, truncation_ok=True, history=HISTORY
            ),
            [ollama_reply("O", "length")],
        ),
    ]


RECORDED_MODELS = RECORDED["qualification"]["openai-pass"]["responses"][0]
RECORDED_TAGS = RECORDED["qualification"]["ollama-pass"]["responses"][0]
# Server-reported counts and durations as Ollama might send them, and worse, as
# JSON text. A real Ollama sends integers; the others are what a parser must survive.
RATE_VALUES = {
    "missing": None,
    "null": "null",
    "zero": "0",
    "zero float": "0.0",
    "one": "1",
    "float": "1.5",
    "negative": "-3",
    "boolean": "true",
    "string": '"12"',
    "huge": "1e30",
    "int64 max": "9223372036854775807",
    "subnormal": "5e-324",
}


def with_counts(body_counts, path):
    """An Ollama reply whose duration/count fields are given as JSON text."""
    fields = "".join(f', "{k}": {v}' for k, v in body_counts.items())
    message = '{"content": "OK", "tool_calls": []}'
    body = '{"message": ' + message + ', "done": true, "done_reason": "stop"' + fields
    return {"path": path, "status": 200, "body": body + "}"}


def line(value):
    """One case on one line of a golden file."""
    return json.dumps(value, sort_keys=True)


RATE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "prompt_processing_seconds",
    "decode_seconds",
    "prefill_tokens_per_second",
    "decode_tokens_per_second",
)


def transport():
    """Request bodies and parsed replies on both wire dialects (M4)."""
    result = {}
    for api, example in (("openai", "openai.json"), ("ollama", "ollama.json")):
        for label, settings in SETTINGS.items():
            for name, call, responses in backend_calls(api):
                with tempfile.TemporaryDirectory() as folder:
                    c = configured(example, LIMITS | settings, folder)
                    replay = WireReplay(responses)
                    with deterministic(), recorded(replay, []):
                        outcome = answered(lambda: call(Backend(c)))
                    outcome["requests"] = replay.requests
                    result[f"backend {api}, {label}: {name}"] = line(outcome)
    for label, settings in SETTINGS.items():
        for conversation in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                extra = {}
                if conversation:
                    extra["tool_schema.conversation"] = [
                        {"role": "user", "content": "Plan a tiny tool."}
                    ]
                c = configured("tool-schema.json", LIMITS | settings | extra, folder)
                responses = RECORDED["tool_schema"]["valid"]["responses"][:2]
                for response in responses:
                    boundary = "openai" if "/v1/" in response["path"] else "ollama"
                    replay = WireReplay([response])
                    with deterministic(), recorded(replay, []):
                        outcome = answered(lambda: list(schema_request(c, boundary)))
                    outcome["requests"] = replay.requests
                    key = f"tool schema {boundary}, {label}"
                    if conversation:
                        key += ", operator conversation"
                    result[key] = line(outcome)
    backend_rates, schema_rates = [], []
    with tempfile.TemporaryDirectory() as folder:
        c = configured("ollama.json", LIMITS, folder)
        s = configured("tool-schema.json", LIMITS, folder)
    for count_name, count in RATE_VALUES.items():
        for duration_name, duration in RATE_VALUES.items():
            fields = {}
            if count is not None:
                fields.update(eval_count=count, prompt_eval_count=count)
            if duration is not None:
                fields.update(eval_duration=duration, prompt_eval_duration=duration)
            case = f"count {count_name}, duration {duration_name}: "
            replay = WireReplay([with_counts(fields, "/api/chat")])
            with deterministic(), recorded(replay, []):
                outcome = answered(lambda: Backend(c).complete("Say OK"))
            if "result" in outcome:
                outcome = [outcome["result"]["metrics"][k] for k in RATE_FIELDS]
            backend_rates.append(case + line(outcome))
            replay = WireReplay([with_counts(fields, "/api/chat")])
            with deterministic(), recorded(replay, []):
                outcome = answered(lambda: list(schema_request(s, "ollama")))
            if "result" in outcome:
                outcome = outcome["result"][2]
            schema_rates.append(case + line(outcome))
    result["rates, backend " + line(RATE_FIELDS)] = backend_rates
    result["rates, tool schema predicted_per_second"] = schema_rates
    return result


def card(root, name, vendor=b"0x1002\n", pci=b"0x744c\n", driver=None, **files):
    """A /sys/class/drm card; attribute contents are bytes, None leaves one out."""
    device = root / name / "device"
    device.mkdir(parents=True)
    for attribute, content in (("vendor", vendor), ("device", pci), *files.items()):
        if content is not None:
            (device / attribute).write_bytes(content)
    if driver is not None:
        target = root.parent / "drivers" / driver
        if driver != "missing":
            target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, device / "driver")


def mixed(root):
    vram = {
        "mem_info_vram_total": b"17163091968\n",
        "mem_info_vram_used": b"1073741824\n",
    }
    card(root, "card0", driver="amdgpu", **vram)
    (root / "card0-DP-1").mkdir()
    (root / "card0-DP-1" / "status").write_text("connected\n")
    card(root, "card1", b"0x8086\n", b"0x56a0\n", "i915", **vram)
    card(root, "card2", b"0x10de\n", b"0x2684\n", "nvidia")
    card(root, "card3", mem_info_vram_total=b"10000", mem_info_vram_used=b"20000")
    card(root, "card4", mem_info_vram_total=b"10000")
    card(root, "card5", mem_info_vram_used=b"not-a-number\n")
    card(
        root,
        "card6",
        mem_info_vram_total=b"123456789012345678901\n",
        mem_info_vram_used=b"5\n",
    )
    card(root, "card7", pci=b"0x74\n", mem_info_vram_used=b"7\n")
    card(root, "card8", b"  0x1002  \n", b"0x7ABC", mem_info_vram_used=b"8\n")
    card(root, "card9", vendor=None, mem_info_vram_used=b"9\n")
    card(root, "card10", pci=None, mem_info_vram_used=b"10\n")
    card(root, "card11", vendor=None, mem_info_vram_used=b"11\n")
    (root / "card11" / "device" / "vendor").mkdir()
    card(root, "card12", b"0x1002" + b" " * 20 + b"junk\n", mem_info_vram_used=b"12")
    card(root, "card13", pci=b"0x744c" + b" " * 12 + b"zz\n", mem_info_vram_used=b"13")
    card(root, "card14", driver="radeon", mem_info_vram_used=b"14\n")
    card(root, "card15", driver="missing", mem_info_vram_used=b"15\n")
    card(root, "card16", mem_info_vram_total=b"0\n", mem_info_vram_used=b"0\n")
    card(root, "card17", mem_info_vram_used=b"+5\n")
    card(root, "card18", mem_info_vram_used=b"  42\n\n")
    card(root, "card19", b"0x1234\n", mem_info_vram_used=b"19\n")
    card(root, "cardX", mem_info_vram_used=b"20\n")
    (root / "card20").mkdir()
    (root / "renderD128").mkdir()


def capped(root):
    """More card* entries than the inventory reads: 60 cards, 9 connectors."""
    for n in range(60):
        card(root, f"card{n}", mem_info_vram_used=str(n).encode())
    for n in range(1, 10):
        (root / f"card0-DP-{n}").mkdir()


def fixtures(root):
    """The trees test_memory_probe and test_targets build."""
    card(root, "card0", mem_info_vram_used=b"1000\n", mem_info_vram_total=b"16000\n")
    card(root, "card1", pci=b"0x1002\n", mem_info_vram_used=b"3000\n")
    card(root, "card2", b"0x10de\n")
    card(root, "card3", b"0x8086", b"0x1234", mem_info_vram_total=b"16384")
    card(root, "card4", mem_info_vram_used=b"not-a-number\n")
    (root / "card4-DP-1").mkdir()


SYSFS_TREES = {
    "mixed": mixed,
    "capped": capped,
    "test fixtures": fixtures,
    "undecodable vendor": lambda root: card(root, "card0", b"0x1002\xff\n"),
    "undecodable used": lambda root: card(root, "card0", mem_info_vram_used=b"12\xff"),
    "undecodable total": lambda root: card(
        root, "card0", mem_info_vram_total=b"\xfe", mem_info_vram_used=b"5"
    ),
    "undecodable after 8 KiB": lambda root: card(
        root, "card0", b"0x1002" + b" " * 10 + b"\n" + b"a" * 9000 + b"\xff\n"
    ),
    "superscript digit": lambda root: card(
        root, "card0", mem_info_vram_used="\u00b2".encode()
    ),
}


def sysfs():
    """Both readers of AMD VRAM under /sys/class/drm (M8)."""
    result = {}
    for name, build in SYSFS_TREES.items():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "drm"
            root.mkdir()
            build(root)
            result[name + ": inventory"] = line(answered(lambda: drm_inventory(root)))
            reader = LinuxReader(4242, Path(folder) / "proc", root)
            result[name + ": memory probe"] = line(answered(reader.vram))
    return result


def stream(*events):
    """A Pi event stream, one JSON value per line."""
    return b"\n".join(json.dumps(e).encode() for e in events)


def tool(name="bash", **extra):
    return {"type": "tool_execution_start", "toolName": name, **extra}


def end(*messages):
    return {"type": "agent_end", "messages": list(messages)}


STOP = {"role": "assistant", "stopReason": "stop"}
VERIFY = tool(args={"command": VERIFIER_COMMAND})
EVENTS = {
    "verifier, stop": stream(VERIFY, end(STOP)),
    "verifier padded, stop": stream(
        tool(args={"command": f"  {VERIFIER_COMMAND}\n"}), end(STOP)
    ),
    "other command, stop": stream(tool(args={"command": "ls /work"}), end(STOP)),
    "read, stop": stream(tool("read", args={}), end(STOP)),
    "three tools, stop": stream(
        tool("read", args={}), VERIFY, tool("write"), end(STOP)
    ),
    "stop only": stream(end(STOP)),
    "verifier only": stream(VERIFY),
    "stop, then length": stream(
        VERIFY, end(STOP), end({"role": "assistant", "stopReason": "length"})
    ),
    "length, then stop": stream(
        VERIFY, end({"role": "assistant", "stopReason": "length"}), end(STOP)
    ),
    "user stop": stream(VERIFY, end({"role": "user", "stopReason": "stop"})),
    "agent_end without messages": stream(VERIFY, {"type": "agent_end"}),
    "messages null": stream(VERIFY, {"type": "agent_end", "messages": None}),
    "message not an object": stream(VERIFY, end("stop")),
    "bash args a list": stream(tool(args=[VERIFIER_COMMAND]), end(STOP)),
    "read args a list": stream(tool("read", args=["x"]), end(STOP)),
    "args null": stream(tool(args=None), end(STOP)),
    "args missing": stream(tool(), end(STOP)),
    "command null": stream(tool(args={"command": None}), end(STOP)),
    "command a number": stream(tool(args={"command": 5}), end(STOP)),
    "type a number": stream({"type": 5}, VERIFY, end(STOP)),
    "line a list": stream(VERIFY, [], end(STOP)),
    "line a number": stream(VERIFY, 5, end(STOP)),
    "blank line": stream(VERIFY) + b"\n\n" + stream(end(STOP)),
    "trailing newline": stream(VERIFY, end(STOP)) + b"\n",
    "CRLF": stream(VERIFY) + b"\r\n" + stream(end(STOP)),
    "not JSON": b"garbage",
    "empty": b"",
    "invalid UTF-8": stream(VERIFY) + b"\n\xff",
    "non-ASCII": stream(tool(args={"command": "echo \u00e9t\u00e9"}), end(STOP)),
}
CODING_RESULTS = {
    "passed": b'{"status":"passed","verifier_exit_code":0}',
    "passed, spaced": b'{ "status": "passed", "verifier_exit_code": 0 }',
    "failed": b'{"status":"failed","verifier_exit_code":1}',
    "exit false": b'{"status":"passed","verifier_exit_code":false}',
    "exit 0.0": b'{"status":"passed","verifier_exit_code":0.0}',
    "extra key": b'{"status":"passed","verifier_exit_code":0,"note":"x"}',
    "list": b"[]",
    "not JSON": b"nope",
    "empty": b"",
}
SATISFIED = {
    "verdict": "satisfied",
    "entry": "epsilon-9",
    "evidence_path": "modules/epsilon-9.md",
    "evidence_line": 3,
}
NOT_SATISFIED = dict.fromkeys(SATISFIED, None) | {"verdict": "not_satisfied"}
INVESTIGATION_RESULTS = {
    name: json.dumps(value).encode()
    for name, value in {
        "satisfied": SATISFIED,
        "not satisfied": NOT_SATISFIED,
        "line 0": SATISFIED | {"evidence_line": 0},
        "line true": SATISFIED | {"evidence_line": True},
        "line 3.0": SATISFIED | {"evidence_line": 3.0},
        "entry empty": SATISFIED | {"entry": ""},
        "path empty": SATISFIED | {"evidence_path": ""},
        "path null": SATISFIED | {"evidence_path": None},
        "not satisfied, with entry": NOT_SATISFIED | {"entry": "epsilon-9"},
        "unknown verdict": SATISFIED | {"verdict": "maybe"},
        "extra key": SATISFIED | {"note": "x"},
        "missing key": {k: v for k, v in SATISFIED.items() if k != "evidence_line"},
        "list": [],
    }.items()
} | {"not JSON": b"nope", "empty": b""}


def completion():
    """Pi self-report checks for coding and investigation (M10)."""
    fixtures = (
        ("coding", CODING_FIXTURE, CODING_RESULTS),
        (
            "investigation findable",
            INVESTIGATION_FIXTURE("findable"),
            INVESTIGATION_RESULTS,
        ),
        (
            "investigation absent",
            INVESTIGATION_FIXTURE("absent"),
            INVESTIGATION_RESULTS,
        ),
    )
    result = {}
    for name, fixture, results in fixtures:
        for events_name, events in EVENTS.items():
            result[f"{name}, {events_name}"] = line(
                {
                    result_name: fixture.completion(events, raw)
                    for result_name, raw in results.items()
                }
            )
    return result


def demo(example, folder):
    """`poundlock-toolkit demo`, end to end: exit code, printed report, document."""
    output, out, err = Path(folder) / "demo.json", io.StringIO(), io.StringIO()
    replay, snapshots = Replay(), []
    with (
        deterministic(),
        recorded(replay, snapshots),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = main(["demo", str(EXAMPLES / example), "--output", str(output)])
    result = stored(output, snapshots)
    result.update(
        exit=code,
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        requests=replay.requests,
    )
    return result


class GoldenTests(unittest.TestCase):
    maxDiff = None

    def assertGolden(self, name, value):
        text = (GOLDEN / f"{name}.json").read_text()
        expected = json.loads(text)
        self.assertEqual(sorted(value), sorted(expected))
        for case in sorted(expected):
            with self.subTest(golden=name, case=case):
                self.assertEqual(canonical(value[case]), canonical(expected[case]))
        self.assertEqual(canonical(value), text)

    def test_tool_schema_runs(self):
        """schema_evaluation (M2): documents, snapshots, requests, progress."""
        self.assertGolden("tool-schema-runs", tool_schema_runs())

    def test_qualification_runs_and_demo(self):
        """evaluation and backends over recorded responses, and the demo."""
        self.assertGolden("qualification-runs", qualification_runs())

    def test_guards_and_signal_handlers(self):
        """The refusals, handlers and clean-up around both evaluators (M3)."""
        self.assertGolden("guards", guards())

    def test_transport(self):
        """The bodies sent and the replies parsed on both dialects (M4)."""
        self.assertGolden("transport", transport())

    def test_sysfs_vram_readers(self):
        """Inventory and memory-probe reads of AMD VRAM (M8)."""
        self.assertGolden("sysfs", sysfs())

    def test_pi_self_report_checks(self):
        """The coding and investigation completion checks (M10)."""
        self.assertGolden("completion", completion())


if __name__ == "__main__":
    unittest.main()

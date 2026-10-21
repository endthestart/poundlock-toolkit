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
from poundlock_toolkit.backends import empty_metrics
from poundlock_toolkit.cli import main
from poundlock_toolkit.core import Failure, config
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.evaluation import evaluate
from poundlock_toolkit.schema_evaluation import evaluate as schema_evaluate

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


if __name__ == "__main__":
    unittest.main()

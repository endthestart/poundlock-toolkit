import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from poundlock_toolkit.core import config, Failure
from poundlock_toolkit.fixtures import CODING_FIXTURE, INVESTIGATION_FIXTURE
from poundlock_toolkit.pi import (
    PiRunner,
    VERIFIER_COMMAND,
    active_compaction_gate,
    canonical_call,
    count_compactions,
    count_loops,
    readable_prefixes,
)


class PiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.c = config(
            Path(__file__).parents[1] / "src/poundlock_toolkit/data/examples/pi.json"
        )

    def run_worker(self, events=None):
        calls = []
        events = events or b"\n".join(
            json.dumps(e).encode()
            for e in [
                {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "args": {"command": VERIFIER_COMMAND},
                },
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                },
            ]
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as t:
            for name, content in [
                ("slugify.py", b'def slugify(text):\n    return "test-only"\n'),
                ("RESULT.json", b'{"status":"passed","verifier_exit_code":0}'),
            ]:
                item = tarfile.TarInfo(name)
                item.size = len(content)
                t.addfile(item, io.BytesIO(content))

        def fake(command, *args, **kwargs):
            calls.append(command)
            if command[1:3] == ["context", "inspect"]:
                return 0, json.dumps(
                    [{"Endpoints": {"docker": {"Host": "unix:///example.sock"}}}]
                ).encode()
            if command[1] == "info":
                return 0, json.dumps(
                    dict.fromkeys(
                        ["MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"], True
                    )
                ).encode()
            if command[1:3] == ["image", "inspect"]:
                return 0, b"[]"
            if command[1:3] == ["network", "inspect"]:
                return 0, b'[{"Internal":true}]'
            if command[1] == "run":
                self.assertIn("--read-only", command)
                self.assertIn("--pull=never", command)
                self.assertIn("65534:65534", command)
                self.assertIn("--log-driver=none", command)
                if "--detach" in command:
                    launch = command[command.index("-c") + 1]
                    compile(launch, "launcher", "exec")
                    mounted = command[command.index("--volume") + 1].removesuffix(
                        ":/fixture:ro"
                    )
                    models = json.loads((Path(mounted) / "models.json").read_text())
                    self.models = models
                    self.settings = json.loads(
                        (Path(mounted) / "settings.json").read_text()
                    )
                    self.limit_ts = (Path(mounted) / "limit.ts").read_text()
                    self.assertEqual(
                        models["providers"]["toolkit"]["models"][0]["samplingParams"][
                            "temperature"
                        ],
                        0,
                    )
                    self.assertIn("before_provider_request", self.limit_ts)
                    return 0, b"container-id"
                self.assertEqual(command[command.index("--network") + 1], "none")
                return 0, b"7 checks passed\n"
            if command[1] == "exec":
                if "tar" in command:
                    return 0, archive.getvalue()
                return (0, events) if command[-1] == "/tmp/events" else (0, b"0\n")
            if command[1] == "rm":
                return 0, b""
            raise AssertionError(command)

        p = PiRunner(self.c)
        with tempfile.TemporaryDirectory() as folder:
            p.journal = Path(folder) / "owned.json"
            with patch("poundlock_toolkit.pi.bounded", side_effect=fake):
                result = p.run()
            self.assertEqual(json.loads(p.journal.read_text())["cleanup"], "passed")
        return p, calls, result

    def test_complete_adapter_contract_without_docker(self):
        p, calls, result = self.run_worker()
        model = self.models["providers"]["toolkit"]["models"][0]
        self.assertFalse(model["reasoning"])
        self.assertNotIn("thinkingLevelMap", model)
        self.assertTrue(result["independent_pass"])
        self.assertTrue(result["completion_pass"])
        self.assertEqual(p.cleanup, "passed")
        self.assertEqual(len([c for c in calls if c[1] == "rm"]), 2)

    def test_reasoning_off_effort_is_sent_explicitly(self):
        self.c["settings"]["reasoning_off_effort"] = "none"
        self.run_worker()
        provider = self.models["providers"]["toolkit"]
        self.assertTrue(provider["compat"]["supportsReasoningEffort"])
        self.assertTrue(provider["models"][0]["reasoning"])
        self.assertEqual(provider["models"][0]["thinkingLevelMap"], {"off": "none"})

    def test_run_defaults_to_the_coding_fixture(self):
        """`run()` with no argument (the pre-fixture call shape) still runs coding."""
        p, calls, result = self.run_worker()
        self.assertTrue(result["independent_pass"])
        detach = next(c for c in calls if c[1] == "run" and "--detach" in c)
        self.assertIn(CODING_FIXTURE.prompt, detach)

    def test_readable_prefixes_generalizes_flat_and_nested_fixtures(self):
        self.assertEqual(
            readable_prefixes(CODING_FIXTURE.worker_files), ["/fixture/verify.py"]
        )
        self.assertEqual(
            readable_prefixes(INVESTIGATION_FIXTURE("findable").worker_files),
            ["/fixture/corpus/"],
        )

    def test_no_remote_engine_or_credentials(self):
        with patch.dict("os.environ", {"DOCKER_HOST": "ssh://example.invalid"}):
            with self.assertRaises(Failure):
                PiRunner(self.c).preflight()
        self.c["credential_env"] = "SECRET_KEY"
        with self.assertRaises(Failure):
            PiRunner(self.c).preflight()

    def test_failure_after_worker_creation_cleans_owned_container(self):
        p = PiRunner(self.c)

        def fake(command, *args, **kwargs):
            if command[1] == "run":
                raise Failure("timeout", "timeout")
            return 0, b""

        with (
            patch.object(p, "preflight"),
            patch("poundlock_toolkit.pi.bounded", side_effect=fake),
        ):
            with self.assertRaises(Failure):
                p.run()
        self.assertEqual(len(p.owned), 1)
        self.assertEqual(p.cleanup, "passed")

    def test_compaction_gate_disabled_by_default(self):
        """The default coding run disables compaction, as before; no report
        contract is injected since the extension substitution is empty."""
        self.run_worker()
        self.assertEqual(self.settings["compaction"], {"enabled": False})
        self.assertEqual(
            self.models["providers"]["toolkit"]["models"][0]["contextWindow"],
            self.c["settings"]["context_tokens"],
        )
        self.assertNotIn("Report contract", self.limit_ts)

    def test_compaction_gate_forces_small_window_and_report_contract(self):
        """The opt-in variant: compaction enabled, a small forced context
        window/reserve, and the report contract embedded for the extension
        to re-assert in the system prompt on every request."""
        self.c["harness"]["compaction_gate"] = {
            "enabled": True,
            "context_tokens": 4096,
            "reserve_tokens": 512,
            "keep_recent_tokens": 512,
        }
        self.run_worker()
        self.assertEqual(
            self.settings["compaction"],
            {"enabled": True, "reserveTokens": 512, "keepRecentTokens": 512},
        )
        self.assertEqual(
            self.models["providers"]["toolkit"]["models"][0]["contextWindow"], 4096
        )
        self.assertIn("Report contract", self.limit_ts)
        # JSON-embedded, so quotes inside the contract are escaped; match the
        # unquoted portion instead of the fixture's raw string.
        self.assertIn("restated because your conversation history", self.limit_ts)

    def test_compaction_gate_never_applies_to_investigation(self):
        """A coding-workload variant only: investigation keeps the default
        (compaction disabled), even with the gate configured on."""
        self.c["harness"]["compaction_gate"] = {
            "enabled": True,
            "context_tokens": 4096,
            "reserve_tokens": 512,
            "keep_recent_tokens": 512,
        }
        investigation_events = b"\n".join(
            json.dumps(e).encode()
            for e in [
                {
                    "type": "tool_execution_start",
                    "toolName": "read",
                    "args": {"path": "corpus/MANIFEST.md"},
                },
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                },
            ]
        )
        calls = []

        def fake(command, *args, **kwargs):
            calls.append(command)
            if command[1:3] == ["context", "inspect"]:
                return 0, json.dumps(
                    [{"Endpoints": {"docker": {"Host": "unix:///example.sock"}}}]
                ).encode()
            if command[1] == "info":
                return 0, json.dumps(
                    dict.fromkeys(
                        ["MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"], True
                    )
                ).encode()
            if command[1:3] == ["image", "inspect"]:
                return 0, b"[]"
            if command[1:3] == ["network", "inspect"]:
                return 0, b'[{"Internal":true}]'
            if command[1] == "run":
                if "--detach" in command:
                    mounted = command[command.index("--volume") + 1].removesuffix(
                        ":/fixture:ro"
                    )
                    self.settings = json.loads(
                        (Path(mounted) / "settings.json").read_text()
                    )
                    self.limit_ts = (Path(mounted) / "limit.ts").read_text()
                    return 0, b"container-id"
                return 0, b"9 checks passed\n"
            if command[1] == "exec":
                if "tar" in command:
                    archive = io.BytesIO()
                    with tarfile.open(fileobj=archive, mode="w") as t:
                        content = json.dumps(
                            {
                                "verdict": "not_satisfied",
                                "entry": None,
                                "evidence_path": None,
                                "evidence_line": None,
                            }
                        ).encode()
                        item = tarfile.TarInfo("RESULT.json")
                        item.size = len(content)
                        t.addfile(item, io.BytesIO(content))
                    return 0, archive.getvalue()
                return (
                    (0, investigation_events)
                    if command[-1] == "/tmp/events"
                    else (0, b"0\n")
                )
            if command[1] == "rm":
                return 0, b""
            raise AssertionError(command)

        p = PiRunner(self.c)
        with tempfile.TemporaryDirectory() as folder:
            p.journal = Path(folder) / "owned.json"
            with patch("poundlock_toolkit.pi.bounded", side_effect=fake):
                p.run(INVESTIGATION_FIXTURE("absent"))
        self.assertEqual(self.settings["compaction"], {"enabled": False})
        self.assertNotIn("Report contract", self.limit_ts)

    def test_compactions_and_loops_recorded_from_transcript(self):
        events = b"\n".join(
            json.dumps(e).encode()
            for e in [
                {"type": "compaction_end"},
                {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "args": {"command": VERIFIER_COMMAND},
                },
                {"type": "compaction_end"},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                },
            ]
        )
        _, _, result = self.run_worker(events=events)
        self.assertEqual(result["metrics"]["compactions"], 2)
        self.assertEqual(result["metrics"]["loops_detected"], 0)


class LoopAndCompactionDetectionTests(unittest.TestCase):
    """Pure-function tests, adapted from an earlier research harness's `coarse_target`/
    `cycle_period` probes, ported to this fixture's small turn budget."""

    def event(self, tool, command):
        return json.dumps(
            {
                "type": "tool_execution_start",
                "toolName": tool,
                "args": {"command": command},
            }
        ).encode()

    def test_count_compactions_counts_only_completed_ones(self):
        events = b"\n".join(
            [
                json.dumps({"type": "compaction_start"}).encode(),
                json.dumps({"type": "compaction_end"}).encode(),
                json.dumps({"type": "compaction_end"}).encode(),
                b"not even json",
            ]
        )
        self.assertEqual(count_compactions(events), 2)

    def test_aborted_or_failed_compaction_is_not_a_compaction(self):
        """Pi emits `compaction_end` for a compaction that was aborted or failed
        too. Nothing was summarized, so it cannot satisfy the compaction gate."""
        end = {"type": "compaction_end", "reason": "threshold", "willRetry": False}
        events = b"\n".join(
            json.dumps(e).encode()
            for e in [
                dict(end, aborted=True),
                dict(end, aborted=False, errorMessage="summarization failed"),
                dict(end, aborted=False),
            ]
        )
        self.assertEqual(count_compactions(events), 1)

    def test_compaction_gate_is_active_only_for_enabled_coding(self):
        gate = {
            "enabled": True,
            "context_tokens": 4096,
            "reserve_tokens": 512,
            "keep_recent_tokens": 512,
        }
        c = {"harness": {"compaction_gate": gate}}
        self.assertEqual(active_compaction_gate(c, "coding"), gate)
        self.assertIsNone(active_compaction_gate(c, "investigation"))
        gate["enabled"] = False
        self.assertIsNone(active_compaction_gate(c, "coding"))
        self.assertIsNone(active_compaction_gate({"harness": {}}, "coding"))

    def test_canonical_call_strips_incidental_cd_prefix(self):
        a = canonical_call({"toolName": "bash", "args": {"command": "cd /work && ls"}})
        b = canonical_call({"toolName": "bash", "args": {"command": "ls"}})
        self.assertEqual(a, b)

    def test_immediately_repeated_identical_command_is_a_loop(self):
        events = b"\n".join(self.event("bash", "ls") for _ in range(6))
        self.assertGreaterEqual(count_loops(events), 1)

    def test_varied_progressing_calls_are_not_a_loop(self):
        events = b"\n".join(self.event("bash", f"cat file-{n}.py") for n in range(6))
        self.assertEqual(count_loops(events), 0)

    def test_a_stuck_stretch_is_counted_once_not_per_call(self):
        events = b"\n".join(self.event("bash", "ls") for _ in range(20))
        self.assertEqual(count_loops(events, window=4, max_period=2), 1)

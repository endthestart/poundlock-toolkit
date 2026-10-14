"""The `investigation` workload: sealed corpus, split worker/verifier mounts,
independent scoring and the paired findable/absent verdict."""

import copy
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poundlock_toolkit.backends import empty_metrics
from poundlock_toolkit.core import config, data, validate
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.evaluation import evaluate
from poundlock_toolkit.fixtures import INVESTIGATION_FIXTURE
from poundlock_toolkit.pi import PiRunner, snapshot
from poundlock_toolkit.planning import plan
from poundlock_toolkit.reporting import (
    investigation_pair,
    recommendation,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "src/poundlock_toolkit/data/verify_investigation.py"

CORRECT = {
    "findable": {
        "verdict": "satisfied",
        "entry": "EPSILON-9",
        "evidence_path": "modules/epsilon-9.md",
        "evidence_line": 9,
    },
    "absent": {
        "verdict": "not_satisfied",
        "entry": None,
        "evidence_path": None,
        "evidence_line": None,
    },
}


def investigation_config():
    return config(ROOT / "examples/investigation.json")


class ToolCapableBackend(SyntheticBackend):
    """A SyntheticBackend whose tool gate actually returns a valid call, so
    investigation's task attempts (which need the same tool gate as coding)
    can be promoted in tests that never touch real inference."""

    def complete(self, prompt, tool=False, stream=False):
        result = super().complete(prompt, tool=tool, stream=stream)
        if tool:
            result["calls"] = [
                {
                    "function": {
                        "name": "record_value",
                        "arguments": {"key": "answer", "value": 123},
                    }
                }
            ]
        return result


class FakePi:
    """Stands in for PiRunner in evaluate(); records which fixture it ran."""

    def __init__(self, c):
        self.c = c
        self.cleanup = "passed"
        self.journal = None
        self.calls = []

    def run(self, fixture=None):
        self.calls.append(fixture)
        record = getattr(self, "next_result", None)
        if record is not None:
            return record
        return {
            "metrics": empty_metrics(),
            "response_sha256": "0" * 64,
            "independent_pass": True,
            "completion_pass": True,
        }


def fake_factory(record=None):
    def factory(c):
        pi = FakePi(c)
        pi.next_result = record
        return pi

    return factory


class FixtureBuildTests(unittest.TestCase):
    def test_answer_key_never_in_worker_files(self):
        for variant in ("findable", "absent"):
            fx = INVESTIGATION_FIXTURE(variant)
            for relpath in fx.worker_files:
                self.assertNotIn("answer-key", relpath)
            self.assertIn("answer-key.json", fx.verifier_files)
            key = json.loads(fx.verifier_files["answer-key.json"])
            self.assertNotIn("evidence_line", key)  # only the hash crosses over

    def test_findable_entry_present_absent_entry_removed(self):
        findable = INVESTIGATION_FIXTURE("findable")
        absent = INVESTIGATION_FIXTURE("absent")
        self.assertEqual(findable.entry_count, 5)
        self.assertEqual(absent.entry_count, 4)
        self.assertIn("corpus/modules/epsilon-9.md", findable.worker_files)
        self.assertNotIn("corpus/modules/epsilon-9.md", absent.worker_files)

    def test_corpus_digest_stable_and_distinguishes_variants(self):
        a = INVESTIGATION_FIXTURE("findable")
        b = INVESTIGATION_FIXTURE("findable")
        c = INVESTIGATION_FIXTURE("absent")
        self.assertEqual(a.corpus_sha256, b.corpus_sha256)
        self.assertNotEqual(a.corpus_sha256, c.corpus_sha256)

    def test_unknown_variant_rejected(self):
        with self.assertRaises(ValueError):
            INVESTIGATION_FIXTURE("nonexistent")

    def test_expected_stdout_and_verify_argv(self):
        fx = INVESTIGATION_FIXTURE("findable")
        self.assertEqual(fx.expected_verifier_stdout, b"9 checks passed")
        self.assertEqual(
            fx.verify_argv(),
            [
                "/fixture/verify_investigation.py",
                "/fixture/RESULT.json",
                "/fixture/answer-key.json",
                "/fixture/corpus",
            ],
        )


class VerifierNegativeControlTests(unittest.TestCase):
    """The independent verifier, invoked exactly as pi.py invokes it."""

    def run_verifier(self, variant, candidate):
        fx = INVESTIGATION_FIXTURE(variant)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "verify_investigation.py").write_text(
                data("verify_investigation.py")
            )
            (root / "answer-key.json").write_text(fx.verifier_files["answer-key.json"])
            corpus = root / "corpus"
            for relpath, text in fx.worker_files.items():
                path = root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (root / "RESULT.json").write_text(json.dumps(candidate))
            p = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(root / "verify_investigation.py"),
                    str(root / "RESULT.json"),
                    str(root / "answer-key.json"),
                    str(corpus),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return p.returncode, p.stdout.strip()

    def test_correct_findable_and_absent_pass(self):
        self.assertEqual(
            self.run_verifier("findable", CORRECT["findable"]), (0, "9 checks passed")
        )
        self.assertEqual(
            self.run_verifier("absent", CORRECT["absent"]), (0, "9 checks passed")
        )

    def test_fabricated_result_file_rejected(self):
        rc, out = self.run_verifier(
            "findable", {"status": "passed", "verifier_exit_code": 0}
        )
        self.assertEqual((rc, out), (1, "verification failed"))

    def test_decoy_entry_id_rejected(self):
        candidate = dict(CORRECT["findable"], entry="DELTA-3")
        rc, _ = self.run_verifier("findable", candidate)
        self.assertEqual(rc, 1)

    def test_right_id_wrong_evidence_path_rejected(self):
        candidate = dict(CORRECT["findable"], evidence_path="catalog/entry-delta.md")
        rc, _ = self.run_verifier("findable", candidate)
        self.assertEqual(rc, 1)

    def test_any_substantive_line_of_the_entry_accepted(self):
        # Which of the entry's own claims is most decisive is a judgement call;
        # pointing at the right entry's real text is the measured capability.
        for line in (5, 7, 9):
            candidate = dict(CORRECT["findable"], evidence_line=line)
            self.assertEqual(self.run_verifier("findable", candidate)[0], 0)

    def test_right_id_wrong_line_rejected(self):
        candidate = dict(CORRECT["findable"], evidence_line=3)
        rc, _ = self.run_verifier("findable", candidate)
        self.assertEqual(rc, 1)

    def test_index_row_naming_the_entry_is_not_evidence(self):
        candidate = dict(
            CORRECT["findable"],
            evidence_path="index/capability-index.md",
            evidence_line=6,
        )
        rc, _ = self.run_verifier("findable", candidate)
        self.assertEqual(rc, 1)

    def test_manifest_lists_every_corpus_file(self):
        for variant in ("findable", "absent"):
            fx = INVESTIGATION_FIXTURE(variant)
            listing = fx.worker_files["corpus/MANIFEST.md"]
            for name in fx.worker_files:
                self.assertIn(name.removeprefix("corpus/"), listing)

    def test_not_satisfied_on_findable_rejected(self):
        rc, _ = self.run_verifier("findable", CORRECT["absent"])
        self.assertEqual(rc, 1)

    def test_satisfied_on_absent_rejected(self):
        rc, _ = self.run_verifier("absent", CORRECT["findable"])
        self.assertEqual(rc, 1)

    def test_evidence_path_traversal_rejected(self):
        candidate = dict(
            CORRECT["findable"], evidence_path="../../../etc/passwd", evidence_line=1
        )
        rc, _ = self.run_verifier("findable", candidate)
        self.assertEqual(rc, 1)


class PiRunnerInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.c = investigation_config()
        self.fixture = INVESTIGATION_FIXTURE("findable")

    def run_worker(self, result_files):
        events = b"\n".join(
            json.dumps(e).encode()
            for e in [
                {"type": "tool_execution_start", "toolName": "read", "args": {}},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "stopReason": "stop"}],
                },
            ]
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as t:
            for name, content in result_files:
                item = tarfile.TarInfo(name)
                item.size = len(content)
                t.addfile(item, io.BytesIO(content))

        mounts = []

        def fake(command, *args, **kwargs):
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
                    mounts.append(("worker", mounted))
                    self.assertFalse((Path(mounted) / "answer-key.json").exists())
                    return 0, b"container-id"
                mounted = command[command.index("--volume") + 1].removesuffix(
                    ":/fixture:ro"
                )
                mounts.append(("verifier", mounted))
                self.assertTrue((Path(mounted) / "answer-key.json").exists())
                return 0, self.fixture.expected_verifier_stdout + b"\n"
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
                result = p.run(self.fixture)
        return p, mounts, result

    def test_answer_key_never_reaches_worker_and_run_succeeds(self):
        result_bytes = json.dumps(CORRECT["findable"]).encode()
        p, mounts, result = self.run_worker([("RESULT.json", result_bytes)])
        self.assertEqual([m[0] for m in mounts], ["worker", "verifier"])
        self.assertTrue(result["independent_pass"])
        self.assertTrue(result["completion_pass"])
        self.assertEqual(p.cleanup, "passed")

    def test_unauthorized_extra_output_file_rejected(self):
        from poundlock_toolkit.core import Failure

        result_bytes = json.dumps(CORRECT["findable"]).encode()
        with self.assertRaises(Failure) as caught:
            self.run_worker(
                [("RESULT.json", result_bytes), ("extra.txt", b"unexpected")]
            )
        self.assertEqual(caught.exception.code, "unauthorized_file")

    def test_symlinked_result_file_rejected(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as t:
            member = tarfile.TarInfo("work/RESULT.json")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            member.size = 0
            t.addfile(member)
        from poundlock_toolkit.core import Failure

        with self.assertRaises(Failure):
            snapshot(raw.getvalue(), 4096, allowed=self.fixture.allowed_outputs)


class GateChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = investigation_config()
        self.c["limits"].update(repetitions=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_investigation_needs_the_same_gates_as_coding(self):
        d = evaluate(
            self.c,
            self.root / "result.json",
            backend=ToolCapableBackend(),
            pi_factory=fake_factory(),
        )
        stages = {a["stage"] for a in d["attempts"]}
        self.assertEqual(
            stages, {"readiness", "warmup", "api", "tool", "performance", "task"}
        )
        task = [a for a in d["attempts"] if a["stage"] == "task"][0]
        self.assertEqual(task["outcome"], "pass")
        self.assertEqual(d["investigation"], plan(self.c)["investigation"])

    def test_failed_tool_gate_blocks_investigation_promotion(self):
        d = evaluate(
            self.c,
            self.root / "result.json",
            backend=SyntheticBackend(),  # no valid tool call -> tool gate fails
            pi_factory=fake_factory(),
        )
        task = [a for a in d["attempts"] if a["stage"] == "task"][0]
        self.assertEqual(task["outcome"], "skipped")

    def test_variant_and_corpus_folded_into_prompt_hash(self):
        d = evaluate(
            self.c,
            self.root / "findable.json",
            backend=ToolCapableBackend(),
            pi_factory=fake_factory(),
        )
        absent_c = copy.deepcopy(self.c)
        absent_c["investigation"]["variant"] = "absent"
        e = evaluate(
            absent_c,
            self.root / "absent.json",
            backend=ToolCapableBackend(),
            pi_factory=fake_factory(),
        )
        task_d = [a for a in d["attempts"] if a["stage"] == "task"][0]
        task_e = [a for a in e["attempts"] if a["stage"] == "task"][0]
        self.assertNotEqual(task_d["prompt_sha256"], task_e["prompt_sha256"])


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = investigation_config()
        self.c["limits"].update(repetitions=3)
        self.c["model"].update(revision="fixed-revision", quantization="Q4_K_M")
        self.c["backend"].update(version="1.0", application=None)
        self.c["settings"].update(
            template_sha256="a" * 64, prefix_cache="disabled", kv_quantization="f16"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_variant(self, variant, name):
        c = copy.deepcopy(self.c)
        c["investigation"]["variant"] = variant
        return evaluate(
            c, self.root / name, backend=ToolCapableBackend(), pi_factory=fake_factory()
        )

    def test_investigation_qualifies_like_coding(self):
        d = self.run_variant("findable", "findable.json")
        r = recommendation(d, "investigation")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["passed"], 3)

    def test_investigation_pair_requires_both_variants_and_matching_identity(self):
        findable = self.run_variant("findable", "findable.json")
        absent = self.run_variant("absent", "absent.json")
        self.assertEqual(findable["identity_sha256"], absent["identity_sha256"])
        pair = investigation_pair(findable, absent)
        self.assertTrue(pair["qualified"])

        broken_absent = copy.deepcopy(absent)
        broken_absent["attempts"][-1].update(
            outcome="task_quality_failure", independent_pass=False
        )
        pair = investigation_pair(findable, broken_absent)
        self.assertFalse(pair["qualified"])

        mismatched = copy.deepcopy(absent)
        mismatched["identity_sha256"] = "0" * 64
        pair = investigation_pair(findable, mismatched)
        self.assertFalse(pair["qualified"])
        self.assertFalse(pair["identity_match"])

    def test_always_not_satisfied_is_a_measured_failure_not_a_majority(self):
        """The `absent` variant is free to a model that always says no; the
        paired count must not let that look like mostly-passing evidence."""
        findable = self.run_variant("findable", "findable.json")
        for attempt in findable["attempts"]:
            if attempt["stage"] == "task":
                attempt.update(outcome="task_quality_failure", independent_pass=False)
        absent = self.run_variant("absent", "absent.json")
        pair = investigation_pair(findable, absent)
        self.assertEqual(pair["findable"]["passed"], 0)
        self.assertEqual(pair["absent"]["level"], "repeated task qualification")
        self.assertFalse(pair["qualified"])


class SchemaAndPlanningTests(unittest.TestCase):
    def test_config_requires_investigation_block_when_workload_selected(self):
        c = investigation_config()
        del c["investigation"]
        with self.assertRaises(ValueError):
            validate(c, "config.schema.json")

    def test_config_rejects_investigation_block_without_the_workload(self):
        from poundlock_toolkit.core import config as load_config

        c = load_config(ROOT / "examples/openai.json")
        c["investigation"] = {"variant": "findable"}
        with self.assertRaises(ValueError):
            validate(c, "config.schema.json")

    def test_legacy_document_without_investigation_block_still_validates(self):
        d = evaluate(
            config(ROOT / "examples/openai.json"),
            Path(tempfile.mkdtemp()) / "r.json",
            backend=SyntheticBackend(),
        )
        self.assertNotIn("investigation", d)
        validate(d)

    def test_planning_blockers_mirror_coding(self):
        c = investigation_config()
        c["harness"]["name"] = "direct"
        self.assertTrue(plan(c)["blockers"])
        c = investigation_config()
        c["credential_env"] = "SOME_KEY"
        self.assertTrue(plan(c)["blockers"])

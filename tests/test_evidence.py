"""Serving-application identity, check statuses and secret-free exported artifacts."""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from poundlock_toolkit.backends import Backend, empty_metrics
from poundlock_toolkit.core import config, validate
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.drift import DriftError, evaluation_check
from poundlock_toolkit.evaluation import evaluate, new_document
from poundlock_toolkit.planning import plan
from poundlock_toolkit.reporting import (
    acceptance_rate,
    export,
    recommendation,
    report,
)
from poundlock_toolkit.targets import start, target_plan
from test_investigation import FakePi, ToolCapableBackend, fake_factory
from test_targets import require_isolated_install
from test_toolkit import WireHandler

ROOT = Path(__file__).resolve().parents[1]
TOSH = {"name": "ToshLLM", "version": "0.87.6"}


def complete_identity(c, application=TOSH):
    c["model"].update(revision="fixed-revision", quantization="Q4_K_M")
    c["backend"].update(version="10830", commit="465e49b9c", application=application)
    c["settings"].update(
        template_sha256="a" * 64, prefix_cache="disabled", kv_quantization="f16"
    )
    return c


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_eval(self, c=None, name="result.json"):
        return evaluate(c or self.c, self.root / name, backend=SyntheticBackend())

    def test_application_release_distinguished_on_shared_backend_build(self):
        older = copy.deepcopy(self.c)
        older["backend"]["application"] = {"name": "ToshLLM", "version": "0.87.4"}
        a, b = self.run_eval(older, "a.json"), self.run_eval(name="b.json")
        self.assertEqual(a["serving"]["backend_commit"], b["serving"]["backend_commit"])
        self.assertNotEqual(a["identity_sha256"], b["identity_sha256"])
        self.assertEqual(b["serving"]["application_version"], "0.87.6")
        text = report([b])
        self.assertIn("Serving application release: ToshLLM 0.87.6.", text)
        self.assertIn("Backend build: 10830; commit: 465e49b9c.", text)
        self.assertEqual(
            evaluation_check(plan(older), plan(self.c))["drifted"],
            ["serving_application"],
        )

    def test_unrecorded_application_is_unknown_and_blocks_qualification(self):
        del self.c["backend"]["application"]
        d = self.run_eval()
        self.assertFalse(d["identity_complete"])
        r = recommendation(d, "extraction")
        self.assertEqual(r["checks"]["serving_application"], "unknown")
        self.assertEqual(r["level"], "insufficient evidence")
        self.assertIn("Freeze missing identity", r["next_test"])
        self.c["backend"]["application"] = {"name": "ToshLLM", "version": None}
        self.assertFalse(self.run_eval(name="v.json")["identity_complete"])

    def test_direct_backend_and_recorded_application_qualify(self):
        for application in (None, TOSH):
            with self.subTest(application=application):
                self.c["backend"]["application"] = application
                d = self.run_eval(name=f"{application is None}.json")
                r = recommendation(d, "extraction")
                self.assertEqual(r["level"], "repeated task qualification")
                self.assertTrue(all(v == "pass" for v in r["checks"].values()))
                self.assertIsNone(r["next_required_check"])

    def test_check_statuses_distinguish_fail_unknown_stale(self):
        d = self.run_eval()
        later = datetime.now(timezone.utc) + timedelta(days=31)
        r = recommendation(d, "extraction", now=later)
        self.assertEqual(r["checks"]["freshness"], "stale")
        self.assertEqual(r["next_required_check"], "freshness")
        self.assertIn("refresh stale evidence", r["next_test"])
        self.assertIn("freshness=stale", report([d], now=later))

        failed = copy.deepcopy(d)
        api = [a for a in failed["attempts"] if a["stage"] == "api"][0]
        api.update(outcome="protocol_failure", code="wrong_output")
        r = recommendation(failed, "extraction")
        self.assertEqual(r["checks"]["gates"], "fail")
        self.assertEqual(r["next_required_check"], "gates")

        missing = copy.deepcopy(d)
        missing["attempts"] = [a for a in missing["attempts"] if a["stage"] != "api"]
        self.assertEqual(
            recommendation(missing, "extraction")["checks"]["gates"], "unknown"
        )

        headroom = copy.deepcopy(d)
        headroom["requirements"]["min_available_memory_bytes"] = 10
        self.assertEqual(
            recommendation(headroom, "extraction")["checks"]["capacity"], "unknown"
        )
        headroom["inventory"]["memory"]["system_available_bytes"] = 5
        self.assertEqual(
            recommendation(headroom, "extraction")["checks"]["capacity"], "fail"
        )

    def test_legacy_evidence_without_serving_field_still_validates(self):
        d = self.run_eval()
        del d["serving"]
        validate(d)
        r = recommendation(d, "extraction")
        self.assertEqual(r["checks"]["serving_application"], "unknown")
        self.assertIn("not recorded by this evidence version", report([d]))

    def test_kv_quantization_and_speculative_settings_are_kept(self):
        """(d): kv_quantization used to be silently stripped at evaluation
        time; it and the new optional `speculative` field now survive into
        the qualification document, under the bumped schema_version."""
        self.c["settings"]["speculative"] = {"type": "draft-mtp", "draft_n_max": 4}
        d = self.run_eval()
        self.assertEqual(d["schema_version"], 2)
        self.assertEqual(d["settings"]["kv_quantization"], "f16")
        self.assertEqual(
            d["settings"]["speculative"], {"type": "draft-mtp", "draft_n_max": 4}
        )
        validate(d)

    def test_schema_version_1_evidence_without_new_fields_still_validates(self):
        """(d): bumping schema_version must not strand older evidence. A
        schema_version 1 document missing every field introduced alongside
        the bump (kv_quantization, speculative, the warm-up stage, the new
        metrics) must still read back as valid."""
        d = self.run_eval()
        d["schema_version"] = 1
        del d["settings"]["kv_quantization"]
        d["attempts"] = [a for a in d["attempts"] if a["stage"] != "warmup"]
        for a in d["attempts"]:
            for key in ("prompt_n", "predicted_n", "draft_n", "draft_n_accepted"):
                del a["metrics"][key]
        validate(d)

    def test_throughput_check_reports_possible_spill_not_a_confirmed_cause(self):
        """(b): a configured minimum decode speed the warm run falls short of
        is reported as a possible explanation, never asserted as the cause."""

        class Slow(SyntheticBackend):
            def complete(s, prompt, **kw):
                v = super().complete(prompt, **kw)
                v["metrics"]["decode_tokens_per_second"] = 2.0
                return v

        self.c["requirements"]["min_decode_tokens_per_second"] = 50
        d = evaluate(self.c, self.root / "slow.json", backend=Slow())
        r = recommendation(d, "extraction")
        self.assertEqual(r["checks"]["throughput"], "fail")
        self.assertTrue(
            any(
                "possible explanation is a VRAM spill" in reason
                for reason in r["reasons"]
            )
        )
        self.assertNotIn("caused by", " ".join(r["reasons"]).lower())

    def test_throughput_check_passes_without_a_configured_requirement(self):
        d = self.run_eval()
        self.assertIsNone(d["requirements"].get("min_decode_tokens_per_second"))
        r = recommendation(d, "extraction")
        self.assertEqual(r["checks"]["throughput"], "pass")

    def test_an_unreported_speed_stays_unknown_where_extraction_can_record_one(self):
        self.c["requirements"]["min_decode_tokens_per_second"] = 50
        r = recommendation(self.run_eval(), "extraction")
        self.assertEqual(r["checks"]["throughput"], "unknown")
        self.assertEqual(r["level"], "insufficient evidence")
        self.assertTrue(
            any("decode speed is unknown" in reason for reason in r["reasons"])
        )
        self.assertEqual(r["notes"], [])


class PiEvidenceCase(unittest.TestCase):
    """Coding evidence built through evaluate() with a stand-in Pi worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/pi.json")
        )
        self.runs = 0

    def tearDown(self):
        self.tmp.cleanup()

    def run_coding(self, compactions, decode_speeds=None, c=None):
        """Evaluate with a worker whose n-th coding run records `compactions[n]`
        (and `decode_speeds[n]`, when given). Every run independently passes."""
        recorded = iter(zip(compactions, decode_speeds or [None] * len(compactions)))

        def factory(c):
            pi = FakePi(c)
            metrics = empty_metrics()
            metrics["compactions"], metrics["decode_tokens_per_second"] = next(recorded)
            pi.next_result = {
                "metrics": metrics,
                "response_sha256": "0" * 64,
                "independent_pass": True,
                "completion_pass": True,
            }
            return pi

        self.runs += 1
        return evaluate(
            c or self.c,
            self.root / f"run-{self.runs}.json",
            backend=ToolCapableBackend(),
            pi_factory=factory,
        )


class CompactionGateEvidenceTests(PiEvidenceCase):
    """The gate exists to show the report contract survives a compaction, so a
    run that never compacted proves nothing about that."""

    def setUp(self):
        super().setUp()
        self.c["harness"]["compaction_gate"] = {
            "enabled": True,
            "context_tokens": 4096,
            "reserve_tokens": 512,
            "keep_recent_tokens": 256,
        }

    def test_document_records_the_gate_only_when_coding_ran_under_it(self):
        d = new_document(self.c)
        self.assertEqual(
            d["compaction_gate"],
            {"context_tokens": 4096, "reserve_tokens": 512, "keep_recent_tokens": 256},
        )
        validate(d)
        self.assertEqual(export(d)["compaction_gate"], d["compaction_gate"])

        off = copy.deepcopy(self.c)
        off["harness"]["compaction_gate"]["enabled"] = False
        no_coding = copy.deepcopy(self.c)
        no_coding["workloads"] = ["extraction"]
        investigation = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/investigation.json")
        )
        investigation["harness"]["compaction_gate"] = dict(
            self.c["harness"]["compaction_gate"]
        )
        del self.c["harness"]["compaction_gate"]
        for c in (off, no_coding, investigation, self.c):
            self.assertNotIn("compaction_gate", new_document(c))

    def test_a_gate_that_never_compacted_is_insufficient_evidence(self):
        d = self.run_coding([0, 0, 0])
        r = recommendation(d, "coding")
        # The runs themselves passed; they just never exercised the gate.
        self.assertEqual((r["passed"], r["attempted"]), (3, 3))
        self.assertEqual(r["level"], "insufficient evidence")
        self.assertEqual(r["checks"]["compaction"], "unknown")
        self.assertEqual(r["next_required_check"], "compaction")
        self.assertEqual(len(r["reasons"]), 1)
        self.assertIn("gate did not trigger a compaction", r["reasons"][0])
        self.assertIn("3 of 3", r["reasons"][0])
        self.assertIn("compaction_gate", r["next_test"])
        text = report([d])
        self.assertIn("did not trigger a compaction", text)
        self.assertIn("compaction=unknown", text)

    def test_every_passed_run_must_have_compacted(self):
        r = recommendation(self.run_coding([2, 0, 1]), "coding")
        self.assertEqual(r["level"], "insufficient evidence")
        self.assertIn("1 of 3", r["reasons"][0])

    def test_an_unrecorded_compaction_count_is_not_a_compaction(self):
        r = recommendation(self.run_coding([1, 1, None]), "coding")
        self.assertEqual(r["checks"]["compaction"], "unknown")
        self.assertIn("1 of 3", r["reasons"][0])

    def test_gate_exercised_in_every_passed_run_qualifies(self):
        r = recommendation(self.run_coding([1, 2, 1]), "coding")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["checks"]["compaction"], "pass")
        self.assertEqual(r["reasons"], [])
        self.assertIsNone(r["next_required_check"])

    def test_a_single_run_without_a_compaction_is_not_a_smoke_pass(self):
        self.c["limits"]["repetitions"] = 1
        r = recommendation(self.run_coding([0]), "coding")
        self.assertEqual(r["level"], "insufficient evidence")
        r = recommendation(self.run_coding([1]), "coding")
        self.assertEqual(r["level"], "observed smoke pass")

    def test_without_the_gate_no_compaction_is_the_default_not_a_gap(self):
        del self.c["harness"]["compaction_gate"]
        d = self.run_coding([0, 0, 0])
        self.assertNotIn("compaction_gate", d)
        r = recommendation(d, "coding")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["checks"]["compaction"], "pass")

    def test_a_gate_that_never_compacted_is_not_qualified(self):
        for compactions, level in (
            ([0, 0, 0], "insufficient evidence"),
            ([1, 1, 1], "repeated task qualification"),
        ):
            with self.subTest(compactions=compactions):
                d = self.run_coding(compactions)
                self.assertEqual(recommendation(d, "coding")["level"], level)

    def test_the_gate_never_constrains_other_workloads(self):
        d = self.run_coding([0, 0, 0])
        r = recommendation(d, "extraction")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["checks"]["compaction"], "pass")


class SpeedFloorScopeTests(PiEvidenceCase):
    """Pi records token counts but no server timing, so a decode-speed floor has
    nothing to be measured against on the Pi workloads."""

    def setUp(self):
        super().setUp()
        self.c["requirements"]["min_decode_tokens_per_second"] = 50

    def test_floor_does_not_turn_a_coding_run_into_insufficient_evidence(self):
        d = self.run_coding([None] * 3)
        coding = [a for a in d["attempts"] if a["workload"] == "coding"]
        self.assertTrue(
            all(a["metrics"]["decode_tokens_per_second"] is None for a in coding)
        )
        r = recommendation(d, "coding")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["checks"]["throughput"], "pass")
        self.assertEqual(r["reasons"], [])
        # Not applied, and said so: the floor is not silently forgotten.
        self.assertEqual(len(r["notes"]), 1)
        self.assertIn("(50 tok/s) was not applied to coding", r["notes"][0])
        self.assertIn("Note: The decode-speed floor (50 tok/s)", report([d]))

    def test_the_same_floor_still_applies_where_a_speed_can_be_recorded(self):
        d = self.run_coding([None] * 3)
        r = recommendation(d, "extraction")
        self.assertEqual(r["checks"]["throughput"], "unknown")
        self.assertEqual(r["level"], "insufficient evidence")
        self.assertEqual(r["notes"], [])

    def test_floor_is_not_applied_to_investigation_either(self):
        c = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/investigation.json")
        )
        c["requirements"]["min_decode_tokens_per_second"] = 50
        d = evaluate(
            c,
            self.root / "investigation.json",
            backend=ToolCapableBackend(),
            pi_factory=fake_factory(),
        )
        r = recommendation(d, "investigation")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertIn("not applied to investigation", " ".join(r["notes"]))

    def test_a_pi_run_that_did_record_a_speed_is_held_to_the_floor(self):
        slow = recommendation(self.run_coding([0] * 3, [2.0, 3.0, 2.5]), "coding")
        self.assertEqual(slow["checks"]["throughput"], "fail")
        self.assertEqual(slow["level"], "insufficient evidence")
        self.assertEqual(slow["notes"], [])
        fast = recommendation(self.run_coding([0] * 3, [80.0, 90.0, 85.0]), "coding")
        self.assertEqual(fast["checks"]["throughput"], "pass")
        self.assertEqual(fast["level"], "repeated task qualification")
        self.assertEqual(fast["notes"], [])

    def test_without_a_floor_there_is_nothing_to_note(self):
        del self.c["requirements"]["min_decode_tokens_per_second"]
        d = self.run_coding([None] * 3)
        self.assertEqual(recommendation(d, "coding")["notes"], [])
        self.assertNotIn("Note:", report([d]))


class DraftAcceptanceTests(unittest.TestCase):
    """Acceptance is derived from the recorded draft counts when the report is
    written, wherever they exist, and never stored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, text):
        return [line for line in text.splitlines() if line.startswith("| task |")]

    def test_rate_is_accepted_over_offered(self):
        rate = acceptance_rate({"draft_n": 600, "draft_n_accepted": 470})
        self.assertAlmostEqual(rate, 470 / 600)
        self.assertEqual(acceptance_rate({"draft_n": 4, "draft_n_accepted": 0}), 0)
        self.assertEqual(acceptance_rate({"draft_n": 4, "draft_n_accepted": 4}), 1)

    def test_missing_zero_or_contradictory_counts_have_no_rate(self):
        for metrics in (
            {},
            {"draft_n": None, "draft_n_accepted": None},
            {"draft_n": 10, "draft_n_accepted": None},
            {"draft_n": None, "draft_n_accepted": 5},
            {"draft_n": 0, "draft_n_accepted": 0},
            {"draft_n": 10, "draft_n_accepted": 11},
        ):
            with self.subTest(metrics=metrics):
                self.assertIsNone(acceptance_rate(metrics))

    def test_report_shows_the_rate_beside_its_counts_and_unknown_elsewhere(self):
        d = evaluate(self.c, self.root / "run.json", backend=SyntheticBackend())
        tasks = [a for a in d["attempts"] if a["stage"] == "task"]
        tasks[0]["metrics"].update(draft_n=600, draft_n_accepted=470)
        tasks[1]["metrics"].update(draft_n=0, draft_n_accepted=0)
        text = report([d])
        self.assertIn("| Draft acceptance |", text)
        first, second, third = self.rows(text)
        self.assertTrue(first.endswith("| 78.3% (470/600) |"), first)
        self.assertTrue(second.endswith("| unknown |"), second)
        self.assertTrue(third.endswith("| unknown |"), third)
        # Derived for the report only: nothing is added to the evidence.
        self.assertNotIn("acceptance", json.dumps(export(d)))

    def test_evidence_recorded_without_draft_counts_still_reports(self):
        d = evaluate(self.c, self.root / "run.json", backend=SyntheticBackend())
        d["schema_version"] = 1
        del d["settings"]["kv_quantization"]
        d["attempts"] = [a for a in d["attempts"] if a["stage"] != "warmup"]
        for a in d["attempts"]:
            for key in ("prompt_n", "predicted_n", "draft_n", "draft_n_accepted"):
                del a["metrics"][key]
        validate(d)
        self.assertTrue(
            all(row.endswith("| unknown |") for row in self.rows(report([d])))
        )


class SecretFreeArtifactTests(unittest.TestCase):
    """Exported plans, results, reports and drift reports carry no credentials."""

    SECRET = "sk-canary-0123456789abcdef"
    CREDENTIAL = "TOOLKIT_CANARY_KEY"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        WireHandler.mode = "normal"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), WireHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/private-path/v1"
        self.c = complete_identity(
            config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        )
        self.c.update(endpoint=self.endpoint, credential_env=self.CREDENTIAL)
        self.c["limits"].update(total_seconds=10, request_seconds=2, repetitions=1)
        self.env = patch.dict(
            os.environ, {self.CREDENTIAL: self.SECRET, "HOME": str(self.root)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def assertSecretFree(self, text):
        for value in (self.SECRET, self.CREDENTIAL, "private-path", self.endpoint):
            self.assertNotIn(value, text)

    def test_local_artifacts_are_secret_free(self):
        reviewed = plan(self.c)
        d = evaluate(self.c, self.root / "run.json", backend=Backend(self.c))
        self.assertTrue(any(a["outcome"] == "pass" for a in d["attempts"]))
        changed = copy.deepcopy(self.c)
        changed["endpoint"] = self.endpoint.replace("private-path", "other-path")
        drift = DriftError([evaluation_check(reviewed, plan(changed))]).report()
        for artifact in (reviewed, export(d), drift):
            self.assertSecretFree(json.dumps(artifact))
        self.assertSecretFree(report([d]))
        self.assertSecretFree((self.root / "run.json").read_text())

    def test_target_plan_and_attach_report_are_secret_free(self):
        require_isolated_install()
        target = {"transport": "local", "host": None, "python": sys.executable}
        self.c["credential_env"] = None
        reviewed = target_plan(self.c, target, True)
        self.assertSecretFree(json.dumps(reviewed))
        reviewed["target_inventory"]["os"] = "Windows"
        from test_drift import reseal

        with self.assertRaises(DriftError) as caught:
            start(self.c, target, reseal(reviewed), self.root / "receipt.json")
        self.assertSecretFree(json.dumps(caught.exception.report()))

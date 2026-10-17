"""Attach-time drift: name the drifted assumption, fail with a distinct exit code."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from poundlock_toolkit.cli import EXIT_DRIFT, main
from poundlock_toolkit.core import config, digest, read_json
from poundlock_toolkit.drift import (
    DriftError,
    capacity_check,
    evaluation_check,
    inventory_check,
    sanitize,
    stable_inventory,
)
from poundlock_toolkit.inspection import inspect
from poundlock_toolkit.planning import plan
from poundlock_toolkit.targets import control, start, target_plan
from test_targets import require_isolated_install
from test_toolkit import WireHandler

ROOT = Path(__file__).resolve().parents[1]


def reseal(reviewed):
    reviewed["stable_inventory_sha256"] = digest(
        stable_inventory(reviewed["target_inventory"])
    )
    return reviewed


class DriftUnitTests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        self.inv = inspect()

    def test_capacity_and_timestamps_do_not_invalidate_stable_identity(self):
        later = copy.deepcopy(self.inv)
        later["observed_at"] = "2030-01-01T00:00:00+00:00"
        later["memory"]["system_available_bytes"] = 12345
        for pool in later["memory"]["gpu_pools"]:
            pool["available_bytes"] = 1
        self.assertEqual(stable_inventory(self.inv), stable_inventory(later))
        self.assertEqual(inventory_check(self.inv, later)["status"], "pass")

    def test_stable_inventory_drift_names_each_assumption(self):
        for key, change, name in [
            (
                "cpu_count",
                lambda i: i.update(cpu_count=(i["cpu_count"] or 0) + 1),
                "cpu_count",
            ),
            (
                "ram",
                lambda i: i["memory"].update(system_ram_bytes=1),
                "memory.system_ram_bytes",
            ),
            (
                "docker",
                lambda i: i["software_present"].update(
                    docker=not i["software_present"]["docker"]
                ),
                "software_present.docker",
            ),
            (
                "gpu",
                lambda i: i["gpu_devices"].append(
                    {
                        "vendor": "amd",
                        "pci_device_id": "0x1234",
                        "driver": "amdgpu",
                        "total_bytes": 8,
                    }
                ),
                "gpu_devices[0].vendor",
            ),
        ]:
            with self.subTest(key):
                observed = copy.deepcopy(self.inv)
                observed["gpu_devices"] = []
                reviewed = copy.deepcopy(observed)
                change(observed)
                result = inventory_check(reviewed, observed)
                self.assertEqual(result["status"], "stale")
                self.assertIn(name, [d["assumption"] for d in result["drifted"]])

    def test_previously_known_inventory_now_unreadable_is_unknown(self):
        reviewed = copy.deepcopy(self.inv)
        reviewed["memory"]["system_ram_bytes"] = 1 << 30
        observed = copy.deepcopy(reviewed)
        observed["memory"]["system_ram_bytes"] = None
        result = inventory_check(reviewed, observed)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["drifted"][0]["assumption"], "memory.system_ram_bytes")

    def test_capacity_pass_fail_unknown(self):
        inv = copy.deepcopy(self.inv)
        self.assertEqual(capacity_check(self.c, inv)["status"], "pass")
        self.c["requirements"]["min_available_memory_bytes"] = 1000
        inv["memory"]["system_available_bytes"] = None
        self.assertEqual(capacity_check(self.c, inv)["status"], "unknown")
        inv["memory"]["system_available_bytes"] = 999
        self.assertEqual(capacity_check(self.c, inv)["status"], "fail")
        inv["memory"]["system_available_bytes"] = 1000
        self.assertEqual(capacity_check(self.c, inv)["status"], "pass")

    def test_reviewed_input_drift_names_config_sections(self):
        reviewed = plan(self.c)
        for mutate, name in [
            (lambda c: c["settings"].update(max_output_tokens=513), "settings"),
            (lambda c: c["model"].update(quantization="Q8_0"), "model"),
            (lambda c: c["backend"].update(version="b10830"), "backend_build"),
            (
                lambda c: c["backend"].update(
                    application={"name": "ToshLLM", "version": "0.87.6"}
                ),
                "serving_application",
            ),
            (lambda c: c["settings"].update(kv_quantization="q8_0"), "settings"),
            (lambda c: c["harness"].update(version="0.85.1"), "harness"),
            (
                lambda c: c.update(endpoint="http://127.0.0.1:9/v1"),
                "endpoint_or_credential_reference",
            ),
        ]:
            with self.subTest(name):
                c = copy.deepcopy(self.c)
                mutate(c)
                result = evaluation_check(reviewed, plan(c))
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["drifted"], [name])
        self.assertEqual(evaluation_check(reviewed, plan(self.c))["status"], "pass")

    def test_plan_from_older_format_is_stale_not_silently_used(self):
        old = plan(self.c)
        del old["reviewed_inputs"]
        self.assertEqual(evaluation_check(old, plan(self.c))["check"], "plan_version")

    def test_sanitize_rejects_unexpected_target_content(self):
        with self.assertRaises(ValueError):
            sanitize([{"check": "free-form", "status": "pass"}])
        with self.assertRaises(ValueError):
            sanitize([{"check": "capacity", "status": "ok"}])
        clean = sanitize(
            [
                {
                    "check": "stable_inventory",
                    "status": "stale",
                    "drifted": [
                        {"assumption": "os", "reviewed": "Linux", "observed": "a b"}
                    ],
                    "extra": "dropped",
                }
            ]
        )
        self.assertEqual(clean[0]["drifted"][0]["observed"], "unrepresentable")
        self.assertNotIn("extra", clean[0])


class CliDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main([str(a) for a in argv])
        return code, out.getvalue()

    def test_evaluate_with_changed_config_exits_with_named_drift(self):
        cfg = self.root / "config.json"
        raw = json.loads(
            (ROOT / "src/poundlock_toolkit/data/examples/openai.json").read_text()
        )
        cfg.write_text(json.dumps(raw))
        self.assertEqual(
            self.cli("plan", cfg, "--output", self.root / "plan.json")[0], 0
        )
        raw["settings"]["temperature"] = 0.5
        cfg.write_text(json.dumps(raw))
        code, out = self.cli(
            "evaluate",
            cfg,
            "--plan",
            self.root / "plan.json",
            "--output",
            self.root / "run.json",
        )
        self.assertEqual(code, EXIT_DRIFT)
        report = json.loads(out)
        self.assertEqual(report["next_required_check"], "reviewed_inputs")
        self.assertEqual(report["checks"][0]["drifted"], ["settings"])
        self.assertIn("re-run plan", report["next"])
        self.assertFalse((self.root / "run.json").exists())

    def test_help_lists_drift_exit_code(self):
        from poundlock_toolkit.cli import parser

        self.assertIn(
            f"{EXIT_DRIFT}    a reviewed assumption drifted", parser().format_help()
        )


class TargetDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_isolated_install()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"HOME": str(self.root)})
        self.env.start()
        self.target = {"transport": "local", "host": None, "python": sys.executable}
        self.c = config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        self.c["limits"].update(total_seconds=10, request_seconds=2, repetitions=1)
        WireHandler.mode = "normal"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), WireHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.c["endpoint"] = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        self.tmp.cleanup()

    def runs(self):
        root = self.root / ".local/state/poundlock-toolkit/runs"
        return list(root.iterdir()) if root.exists() else []

    def test_target_inventory_drift_refused_on_target_and_receipt_released(self):
        reviewed = target_plan(self.c, self.target, True)
        present = reviewed["target_inventory"]["software_present"]
        present["docker"] = not present["docker"]
        receipt = self.root / "receipt.json"
        with self.assertRaises(DriftError) as caught:
            start(self.c, self.target, reseal(reviewed), receipt)
        report = caught.exception.report()
        self.assertEqual(report["next_required_check"], "stable_inventory")
        drifted = [c for c in report["checks"] if c["check"] == "stable_inventory"][0][
            "drifted"
        ]
        self.assertEqual(
            [d["assumption"] for d in drifted], ["software_present.docker"]
        )
        self.assertFalse(receipt.exists())
        self.assertEqual(self.runs(), [])

    def test_changed_free_memory_and_timestamp_still_attach(self):
        reviewed = target_plan(self.c, self.target, True)
        reviewed["target_inventory"]["observed_at"] = "2020-01-01T00:00:00+00:00"
        reviewed["target_inventory"]["memory"]["system_available_bytes"] = 1
        receipt = self.root / "receipt.json"
        result = start(self.c, self.target, reviewed, receipt)
        self.assertEqual(result["state"]["status"], "completed")
        self.assertEqual(
            {c["check"]: c["status"] for c in result["attach_checks"]},
            {
                "implementation": "pass",
                "reviewed_inputs": "pass",
                "stable_inventory": "pass",
                "capacity": "pass",
            },
        )
        saved = read_json(receipt)
        self.assertEqual(saved["plan_sha256"], digest(reviewed))
        self.assertEqual(saved["config_sha256"], digest(self.c))
        control(self.target, saved, "remove")

    def test_insufficient_or_unknown_capacity_fails_separately(self):
        self.c["requirements"]["min_available_memory_bytes"] = 1 << 60
        reviewed = target_plan(self.c, self.target, True)
        with self.assertRaises(DriftError) as caught:
            start(self.c, self.target, reviewed, self.root / "receipt.json")
        checks = {c["check"]: c["status"] for c in caught.exception.checks}
        self.assertEqual(checks["stable_inventory"], "pass")
        self.assertIn(checks["capacity"], ("fail", "unknown"))

    def test_edited_inventory_without_reseal_is_invalid(self):
        reviewed = target_plan(self.c, self.target, True)
        reviewed["target_inventory"]["cpu_count"] = 1024
        with self.assertRaises(ValueError) as caught:
            start(self.c, self.target, reviewed, self.root / "receipt.json")
        self.assertNotIsInstance(caught.exception, DriftError)

    def test_previous_target_plan_format_is_stale(self):
        reviewed = target_plan(self.c, self.target, True)
        reviewed["target_plan_version"] = 1
        with self.assertRaises(DriftError) as caught:
            start(self.c, self.target, reviewed, self.root / "receipt.json")
        self.assertEqual(
            caught.exception.report()["next_required_check"], "plan_version"
        )

    def test_receipts_bound_to_reviewed_plan_and_package(self):
        reviewed = target_plan(self.c, self.target, True)
        receipt = self.root / "receipt.json"
        start(self.c, self.target, reviewed, receipt)
        saved = read_json(receipt)
        with self.assertRaises(DriftError) as caught:
            control(self.target, dict(saved, implementation="0" * 64), "status")
        self.assertEqual(caught.exception.checks[0]["check"], "implementation")
        with self.assertRaises(RuntimeError):
            control(self.target, dict(saved, plan_sha256="0" * 64), "status")
        control(self.target, saved, "remove")

    def test_cli_target_run_drift_exit_code(self):
        target = self.root / "target.json"
        target.write_text(json.dumps(self.target))
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps(self.c))
        reviewed = target_plan(self.c, self.target, True)
        reviewed["target_inventory"]["architecture"] = "sparc"
        (self.root / "plan.json").write_text(json.dumps(reseal(reviewed)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(
                [
                    "target-run",
                    str(target),
                    str(cfg),
                    "--plan",
                    str(self.root / "plan.json"),
                    "--receipt",
                    str(self.root / "receipt.json"),
                    "--output",
                    str(self.root / "result.json"),
                    "--execute",
                ]
            )
        self.assertEqual(code, EXIT_DRIFT)
        report = json.loads(out.getvalue()[out.getvalue().rindex('{\n  "state"') :])
        self.assertEqual(report["state"], "attach_checks_failed")
        inventory = [c for c in report["checks"] if c["check"] == "stable_inventory"]
        self.assertEqual(inventory[0]["drifted"][0]["assumption"], "architecture")
        self.assertFalse((self.root / "receipt.json").exists())

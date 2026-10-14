"""Strict schema scoring, wire dialects, privacy and finite target integration."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from poundlock_toolkit.core import Failure, config, digest, read_json, validate
from poundlock_toolkit.evaluation import recover
from poundlock_toolkit.planning import plan
from poundlock_toolkit.reporting import export, report
from poundlock_toolkit.schema_evaluation import (
    evaluate,
    request,
    summaries,
    trial_score,
)
from poundlock_toolkit.targets import target_plan, start, control
from poundlock_toolkit.tool_schema import SCHEMA, contract, fixture, score, strict_json

ROOT = Path(__file__).resolve().parents[1]
SECRET = "private-content-must-not-appear"


def valid_arguments():
    return {
        "summary": SECRET,
        "acceptanceCriteria": ["works"],
        "verificationCommands": ["npm test"],
        "tasks": [
            {
                "id": "one",
                "title": "Create storage",
                "objective": "Persist books",
                "workerPrompt": SECRET,
                "acceptanceCriteria": "Round trip works",
                "contextNote": "Existing app",
                "difficulty": "small",
                "dependsOn": [],
                "allowedPaths": ["src/"],
                "verificationCommands": ["npm test"],
            }
        ],
    }


def calls(arguments):
    return [
        {
            "type": "function",
            "function": {"name": "propose_plan", "arguments": arguments},
        }
    ]


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.c = config(ROOT / "examples/tool-schema.json")
        self.c["limits"].update(total_seconds=10, request_seconds=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_native_and_outer_json(self):
        for args in (valid_arguments(), json.dumps(valid_arguments())):
            result = score(args)
            self.assertTrue(result["outer_parse_success"])
            self.assertTrue(result["schema_valid"])
            self.assertEqual(result["errors"], [])

    def test_nested_strings_never_repaired(self):
        for bad, diagnostic in [
            ("[]", "valid_array"),
            ("{}", "valid_object"),
            ('"text"', "valid_string"),
            ("[broken", "malformed"),
        ]:
            with self.subTest(bad=bad):
                args = valid_arguments()
                args["tasks"] = bad
                result = score(args)
                self.assertTrue(result["outer_parse_success"])
                self.assertFalse(result["schema_valid"])
                self.assertEqual(
                    result["errors"][0],
                    {
                        "path": "$.tasks",
                        "expected": "array",
                        "actual": "string",
                        "keyword": "type",
                        "nested_json": diagnostic,
                    },
                )
                self.assertEqual(args["tasks"], bad)

    def test_nested_wrong_objects_and_missing_fields(self):
        for field in ("dependsOn", "allowedPaths", "verificationCommands"):
            for bad, actual in [("[]", "string"), ({}, "object")]:
                args = valid_arguments()
                args["tasks"][0][field] = bad
                errors = score(args)["errors"]
                self.assertEqual(errors[0]["path"], "$.tasks[0]." + field)
                self.assertEqual(errors[0]["actual"], actual)
        args = valid_arguments()
        del args["acceptanceCriteria"]
        del args["tasks"][0]["objective"]
        errors = score(args)["errors"]
        self.assertEqual(
            {e["path"] for e in errors},
            {"$.acceptanceCriteria", "$.tasks[0].objective"},
        )
        self.assertTrue(all(e["actual"] == "missing" for e in errors))
        args = valid_arguments()
        args["tasks"] = {}
        self.assertEqual(score(args)["errors"][0]["actual"], "object")

    def test_outer_parse_and_envelope_failures(self):
        for raw in ("{bad", '{"tasks":[],"tasks":[]}', '{"x":NaN}', '{"x":1e999}'):
            self.assertFalse(score(raw)["outer_parse_success"])
            with self.assertRaises(ValueError):
                strict_json(raw)
        self.assertTrue(score("[]")["outer_parse_success"])
        self.assertFalse(score("[]")["schema_valid"])
        for value, code in [
            ([], "missing_call"),
            (calls({}) * 2, "multiple_calls"),
            ([{"function": {"name": "propose_plan"}}], "missing_arguments"),
            ([None], "malformed_call"),
        ]:
            result, actual = trial_score(value, True)
            self.assertEqual(actual, code)
            self.assertFalse(result["valid_call"])
        result, code = trial_score(calls(valid_arguments()), False)
        self.assertFalse(result["valid_call"])
        self.assertTrue(result["schema_valid"])
        self.assertEqual(code, "missing_completion")
        wrong = calls(valid_arguments())
        wrong[0]["function"]["name"] = SECRET
        self.assertEqual(trial_score(wrong, True)[1], "wrong_tool")

    def test_plan_and_private_conversation_binding(self):
        before = plan(self.c)
        # 3 trials + 1 unscored warm-up, per boundary, over 2 boundaries.
        self.assertEqual(before["maximum_direct_requests"], 8)
        self.c["tool_schema"]["conversation"] = [{"role": "user", "content": SECRET}]
        after = plan(self.c)
        self.assertNotEqual(before["config_sha256"], after["config_sha256"])
        self.assertNotEqual(
            before["tool_schema"]["conversation_sha256"],
            after["tool_schema"]["conversation_sha256"],
        )
        self.assertNotIn(SECRET, json.dumps(after))
        self.c["tool_schema"]["mode"] = "qualification"
        del self.c["tool_schema"]["conversation"]
        self.assertGreater(len(json.dumps(fixture(self.c))), 7000)
        self.assertEqual(contract(self.c)["mode"], "qualification")
        self.c["paid"] = True
        self.assertTrue(plan(self.c)["blockers"])
        with self.assertRaises(ValueError):
            plan(self.c, True)

    def test_scenario_config_constraints_and_endpoints(self):
        for mutation in ("mixed", "pi", "missing", "url"):
            c = copy.deepcopy(self.c)
            if mutation == "mixed":
                c["workloads"].append("coding")
            if mutation == "pi":
                c["harness"]["name"] = "pi"
            if mutation == "missing":
                del c["tool_schema"]
            if mutation == "url":
                c["tool_schema"]["endpoints"]["ollama"] = (
                    "https://user:password@example.invalid"
                )
            path = self.root / "bad.json"
            path.write_text(json.dumps(c))
            with self.assertRaises(ValueError):
                config(path)

    def test_failed_trials_continue_and_exports_are_structural(self):
        def response(c, boundary):
            args = valid_arguments()
            if boundary == "openai":
                args["tasks"] = json.dumps(args["tasks"])
            args[SECRET] = SECRET
            return calls(args), True, None

        doc = evaluate(self.c, self.root / "result.json", requester=response)
        self.assertEqual(len(doc["attempts"]), 6)
        self.assertTrue(all(row["started_at"] for row in doc["attempts"]))
        validate(doc)
        self.assertEqual(doc["summary"]["openai"]["valid_call_rate"], 0)
        self.assertEqual(doc["summary"]["ollama"]["attempted"], 3)
        # (c): every schema-invalid-but-attempted trial here is "invalid", not
        # timed out or unstarted -- the split still adds up to `attempted`.
        for boundary in ("openai", "ollama"):
            s = doc["summary"][boundary]
            self.assertEqual(s["valid"], s["passed"])
            self.assertEqual(s["timed_out"], 0)
            self.assertEqual(s["not_run"], s["planned"] - s["attempted"])
            self.assertEqual(s["invalid"], s["attempted"] - s["valid"] - s["timed_out"])
        # One unscored warm-up per boundary, excluded from the summary.
        self.assertEqual({w["boundary"] for w in doc["warmup"]}, {"openai", "ollama"})
        self.assertNotIn(SECRET, json.dumps(export(doc)))
        self.assertNotIn(SECRET, report([doc]))
        self.assertIn("native Ollama", report([doc]))
        self.assertIn("$.tasks", report([doc]))
        altered = copy.deepcopy(doc)
        altered["attempts"][0]["errors"][0]["path"] = "$.secret-label"
        with self.assertRaises(ValueError):
            export(altered)
        altered = copy.deepcopy(doc)
        altered["raw_response"] = SECRET
        with self.assertRaises(ValueError):
            export(altered)

    def test_warmup_can_be_disabled(self):
        self.c["tool_schema"]["warmup_enabled"] = False
        self.assertFalse(contract(self.c)["warmup_enabled"])

        def response(c, boundary):
            return calls(valid_arguments()), True, None

        doc = evaluate(self.c, self.root / "no_warmup.json", requester=response)
        self.assertEqual(doc["warmup"], [])
        self.assertEqual(
            plan(self.c)["maximum_direct_requests"],
            self.c["tool_schema"]["trials"] * len(self.c["tool_schema"]["endpoints"]),
        )

    def test_timed_out_trials_are_not_counted_as_invalid(self):
        """(c): the bug this fixes -- every trial timing out on a slow but
        correct model used to read as an undifferentiated "0/5"."""

        def timeout(*_):
            raise Failure("timeout", "timeout")

        self.c["tool_schema"]["endpoints"] = {"openai": self.c["endpoint"]}
        doc = evaluate(self.c, self.root / "timeouts.json", requester=timeout)
        s = doc["summary"]["openai"]
        self.assertEqual(s["timed_out"], s["attempted"])
        self.assertEqual(s["invalid"], 0)
        self.assertEqual(s["valid"], 0)
        self.assertIn("timed out", report([doc]))

    def test_predicted_per_second_recorded_per_trial(self):
        def response(c, boundary):
            return calls(valid_arguments()), True, 42.5

        doc = evaluate(self.c, self.root / "speed.json", requester=response)
        self.assertTrue(all(a["predicted_per_second"] == 42.5 for a in doc["attempts"]))
        self.assertTrue(all(w["predicted_per_second"] == 42.5 for w in doc["warmup"]))

    def test_cancel_timeout_and_recovery_preserve_denominators(self):
        def stop(*_):
            raise KeyboardInterrupt()

        doc = evaluate(self.c, self.root / "cancel.json", requester=stop)
        # The warm-up phase runs first and hits the same requester, so
        # cancellation lands there; every scored trial is then skipped.
        self.assertEqual(doc["warmup"][0]["outcome"], "cancelled")
        self.assertEqual(sum(r["outcome"] == "skipped" for r in doc["attempts"]), 6)
        self.assertEqual(
            doc["summary"]["openai"]["attempted"], 0
        )  # native alphabetically first
        interrupted = copy.deepcopy(doc)
        interrupted["attempts"][0].update(
            outcome="running", code="pending", completed_at=None
        )
        recovered = recover(interrupted)
        self.assertEqual(recovered["attempts"][0]["outcome"], "interrupted")
        self.assertEqual(recovered["summary"], summaries(recovered))

        def timeout(*_):
            raise Failure("timeout", "timeout")

        doc = evaluate(self.c, self.root / "timeout.json", requester=timeout)
        self.assertEqual(sum(r["outcome"] == "timeout" for r in doc["attempts"]), 6)
        self.assertEqual(doc["summary"]["openai"]["valid_call_rate"], 0)

    def test_budget_exhaustion_and_actual_alarm(self):
        import time

        self.c["limits"]["request_seconds"] = 1
        self.c["tool_schema"]["trials"] = 1
        self.c["tool_schema"]["endpoints"] = {"openai": self.c["endpoint"]}

        def slow(*_):
            time.sleep(2)
            return calls(valid_arguments()), True, None

        doc = evaluate(self.c, self.root / "alarm.json", requester=slow)
        self.assertEqual(doc["attempts"][0]["outcome"], "timeout")
        self.assertLess(doc["attempts"][0]["latency_seconds"], 1.8)
        with patch(
            # One deadline computation, then one exhausted-budget check for the
            # warm-up row and one for the single scored trial.
            "poundlock_toolkit.schema_evaluation.time.monotonic",
            side_effect=[0, 11, 11],
        ):
            doc = evaluate(self.c, self.root / "budget.json", requester=slow)
        self.assertEqual(doc["attempts"][0]["code"], "budget_exhausted")
        self.assertIsNone(doc["summary"]["openai"]["valid_call_rate"])

    def test_large_diagnostics_are_bounded(self):
        value = valid_arguments()
        value["tasks"] = [{}] * 1000
        result = score(value)
        self.assertFalse(result["schema_valid"])
        self.assertLessEqual(len(result["errors"]), 64)
        self.assertTrue(result["errors_truncated"])
        self.assertEqual(
            SCHEMA["properties"]["tasks"]["items"]["properties"]["dependsOn"]["type"],
            "array",
        )


class SchemaWireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.payloads = []
        self.mode = "valid"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                owner.payloads.append((self.path, payload))
                args = valid_arguments()
                if owner.mode == "nested":
                    args["tasks"] = "[broken"
                if owner.mode == "oversize":
                    args["summary"] = "x" * 20000
                message = {
                    "tool_calls": calls(
                        json.dumps(args)
                        if self.path.endswith("/chat/completions")
                        else args
                    )
                }
                response = (
                    {"choices": [{"message": message, "finish_reason": "tool_calls"}]}
                    if self.path.endswith("/chat/completions")
                    else {"message": message, "done": True, "done_reason": "stop"}
                )
                raw = (
                    b"{broken"
                    if owner.mode == "malformed"
                    else json.dumps(response).encode()
                )
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.c = config(ROOT / "examples/tool-schema.json")
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.c["endpoint"] = endpoint + "/v1"
        self.c["tool_schema"].update(
            trials=1, endpoints={"openai": endpoint + "/v1", "ollama": endpoint}
        )
        self.c["limits"].update(total_seconds=10, request_seconds=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_both_wire_dialects_and_parameters(self):
        doc = evaluate(self.c, self.root / "wire.json")
        self.assertTrue(all(r["valid_call"] for r in doc["attempts"]))
        payloads = dict(self.payloads)
        self.assertEqual(payloads["/v1/chat/completions"]["tool_choice"], "required")
        self.assertEqual(payloads["/api/chat"]["options"]["num_ctx"], 16384)
        for payload in payloads.values():
            self.assertEqual(payload["tools"][0]["function"]["parameters"], SCHEMA)
            self.assertEqual(payload["messages"], fixture(self.c))
        self.assertEqual(doc["summary"]["openai"]["valid_call_rate"], 1)
        self.mode = "nested"
        doc = evaluate(self.c, self.root / "invalid.json")
        self.assertTrue(all(not r["valid_call"] for r in doc["attempts"]))
        self.assertTrue(
            all(r["errors"][0]["nested_json"] == "malformed" for r in doc["attempts"])
        )

    def test_malformed_and_response_limits(self):
        for mode, code in [
            ("malformed", "malformed_response"),
            ("oversize", "response_limit"),
        ]:
            self.mode = mode
            self.c["limits"]["response_bytes"] = 1024
            with self.assertRaises(Failure) as caught:
                request(self.c, "openai")
            self.assertEqual(caught.exception.code, code)

    def test_real_target_process_result_and_retrieval(self):
        from test_targets import require_isolated_install

        require_isolated_install()
        target = {"transport": "local", "host": None, "python": sys.executable}
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            reviewed = target_plan(self.c, target)
            receipt = self.root / "receipt.json"
            response = start(self.c, target, reviewed, receipt)
            self.assertEqual(response["state"]["status"], "completed")
            self.assertEqual(response["result"]["summary"]["openai"]["passed"], 1)
            self.assertEqual(response["result_sha256"], digest(response["result"]))
            retrieved = control(target, read_json(receipt), "status")
            self.assertEqual(retrieved["result_sha256"], response["result_sha256"])
            control(target, read_json(receipt), "remove")

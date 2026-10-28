import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from poundlock_toolkit.backends import Backend
from poundlock_toolkit.core import Failure, config, data, read_json, validate
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.evaluation import evaluate, new_document, recover
from poundlock_toolkit.fixtures import CODING_FIXTURE
from poundlock_toolkit.pi import PiRunner, bounded, snapshot
from poundlock_toolkit.planning import plan, time_feasibility_warnings
from poundlock_toolkit.cli import EXIT_CONSENT, EXIT_OK, parser
from poundlock_toolkit.reporting import export, next_useful_test, recommendation, report
from poundlock_toolkit.workloads import (
    EXTRACTION,
    EXPECTED,
    extraction_pass,
    fenced_json,
    tool_pass,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture_config():
    return config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = fixture_config()

    def tearDown(self):
        self.tmp.cleanup()

    def run_eval(self, backend=None, **kwargs):
        return evaluate(
            self.c,
            self.root / "result.json",
            backend=backend or SyntheticBackend(),
            **kwargs,
        )

    def test_config_rejects_unknown_fields(self):
        self.c["extra"] = {"secret": "should-not-export"}
        with self.assertRaises(ValueError):
            validate(self.c, "config.schema.json")

    def test_config_credentials_not_allowed_in_url(self):
        for url in (
            "https://user:password@example.org/v1",
            "file:///tmp/x",
            "http://example.org/v1?api_key=x",
        ):
            with self.subTest(url=url):
                self.c["endpoint"] = url
                p = self.root / "config.json"
                p.write_text(json.dumps(self.c))
                with self.assertRaises(ValueError):
                    config(p)

    def test_plan_is_bounded_and_has_no_endpoint(self):
        p = plan(self.c)
        self.assertEqual(p["retries"], 0)
        self.assertEqual(p["concurrency"], 1)
        self.assertNotIn("endpoint", p)
        # 3 base + 1 unscored warm-up + 3 extraction repetitions.
        self.assertEqual(p["maximum_direct_requests"], 7)
        self.assertIn("warmup (unscored)", p["stages"])
        self.assertEqual(p["warnings"], [])

    def test_warmup_can_be_disabled_and_drift_covers_the_toggle(self):
        self.c["limits"]["warmup_enabled"] = False
        p = plan(self.c)
        self.assertNotIn("warmup (unscored)", p["stages"])
        # Same accounting as before warm-up existed.
        self.assertEqual(p["maximum_direct_requests"], 6)
        d = self.run_eval()
        self.assertEqual([a for a in d["attempts"] if a["stage"] == "warmup"], [])

    def test_warmup_attempt_recorded_and_excluded_from_scoring(self):
        d = self.run_eval()
        warmup = [a for a in d["attempts"] if a["stage"] == "warmup"]
        self.assertEqual(len(warmup), 1)
        self.assertEqual(warmup[0]["outcome"], "pass")
        self.assertIsNone(warmup[0]["independent_pass"])
        self.assertIsNone(warmup[0]["completion_pass"])
        # `recommendation` only ever looks at stage == "task" attempts.
        r = recommendation(d, "extraction")
        self.assertEqual(r["planned"], self.c["limits"]["repetitions"])

    def test_time_feasibility_warning_fires_for_slow_required_speed(self):
        limits = {"request_seconds": 60, "total_seconds": 300}
        self.assertEqual(time_feasibility_warnings(4096, None, limits), [])
        warnings = time_feasibility_warnings(4096, 10, limits)
        self.assertTrue(
            any("exceeding the configured per-request cap" in w for w in warnings)
        )

    def test_paid_unknown_external_blocked(self):
        for field, value in [
            ("paid", True),
            ("paid", None),
            ("data_destination", "external"),
            ("data_destination", "unknown"),
        ]:
            with self.subTest(field=field, value=value):
                c = copy.deepcopy(self.c)
                c[field] = value
                self.assertTrue(plan(c)["blockers"])

    def test_repeated_results_preserve_every_sample(self):
        d = self.run_eval()
        validate(d)
        # readiness + warmup + api + performance + 3 extraction repetitions.
        self.assertEqual(len(d["attempts"]), 7)
        self.assertEqual(recommendation(d, "extraction")["passed"], 3)
        self.assertFalse(d["identity_complete"])

    def test_synthetic_never_qualifies(self):
        d = self.run_eval(synthetic=True)
        self.assertEqual(
            recommendation(d, "extraction")["level"], "insufficient evidence"
        )
        self.assertIn("Synthetic demonstration", report([d]))

    def test_wrong_extraction_stops_promotion(self):
        class Wrong(SyntheticBackend):
            def complete(s, prompt, **kw):
                v = super().complete(prompt, **kw)
                if prompt == EXTRACTION:
                    v["text"] = '{"quantity":8}'
                return v

        d = self.run_eval(Wrong())
        rows = [a for a in d["attempts"] if a["stage"] == "task"]
        self.assertEqual(
            [a["outcome"] for a in rows], ["task_quality_failure", "skipped", "skipped"]
        )

    def test_repeated_failure_not_hidden_or_retried(self):
        class Flaky(SyntheticBackend):
            n = 0

            def complete(s, prompt, **kw):
                s.n += 1
                # 5th complete() call: warmup, api, performance, task rep 1, then this one.
                if s.n == 5:
                    raise Failure("infrastructure_failure", "unavailable")
                return super().complete(prompt, **kw)

        d = self.run_eval(Flaky())
        r = recommendation(d, "extraction")
        self.assertEqual((r["passed"], r["attempted"]), (2, 3))
        self.assertEqual(r["level"], "insufficient evidence")

    def test_timeout_persisted(self):
        self.c["limits"]["request_seconds"] = 1

        class Slow(SyntheticBackend):
            def readiness(s):
                time.sleep(2)

        d = self.run_eval(Slow())
        self.assertEqual(d["attempts"][0]["outcome"], "timeout")
        self.assertEqual(read_json(self.root / "result.json"), d)

    def test_cancellation_preserves_evidence_and_skips_remaining(self):
        class Cancel(SyntheticBackend):
            def readiness(s):
                raise KeyboardInterrupt()

        d = self.run_eval(Cancel())
        self.assertEqual(d["attempts"][0]["outcome"], "cancelled")
        self.assertTrue(all(a["outcome"] == "skipped" for a in d["attempts"][1:]))

    def test_recover_never_claims_success(self):
        from poundlock_toolkit.evaluation import attempt

        d = new_document(self.c)
        d["attempts"] = [attempt("readiness", "readiness")]
        r = recover(d)
        self.assertEqual(r["attempts"][0]["outcome"], "interrupted")
        self.assertEqual(d["attempts"][0]["outcome"], "running")

    def test_export_allowlist_and_secret_canaries(self):
        secret = "SECRET-canary-freeform"
        self.c["model"]["id"] = secret
        self.c["model"]["revision"] = secret
        self.c["endpoint"] = "https://private.invalid/v1"
        self.c["credential_env"] = "PRIVATE_KEY"
        d = self.run_eval()
        encoded = json.dumps(export(d))
        for value in (secret, "private.invalid", "PRIVATE_KEY"):
            self.assertNotIn(value, encoded)
        d["attempts"][0]["error"] = secret
        with self.assertRaises(ValueError):
            export(d)

    def test_missing_metrics_stay_null(self):
        d = self.run_eval()
        self.assertIsNone(d["attempts"][1]["metrics"]["decode_tokens_per_second"])
        self.assertIn("unknown", report([d]))

    def test_a_code_fence_is_a_formatting_deviation_not_a_failure(self):
        body = json.dumps(EXPECTED)
        for fenced in [f"```json\n{body}\n```", f"```\n{body}\n```"]:
            self.assertTrue(extraction_pass(fenced))
            self.assertTrue(fenced_json(fenced))
        self.assertFalse(fenced_json(body))
        # Prose around the fence is still a failure: the ask was JSON only.
        self.assertFalse(extraction_pass(f"Here you go:\n```json\n{body}\n```"))

    def test_wrong_output_controls(self):
        self.assertTrue(extraction_pass(json.dumps(EXPECTED)))
        for value in [
            "{}",
            "[]",
            "not json",
            json.dumps(dict(EXPECTED, quantity=True)),
            json.dumps(dict(EXPECTED, expedited="false")),
        ]:
            self.assertFalse(extraction_pass(value))
        self.assertFalse(
            tool_pass([{"function": {"name": "record_value", "arguments": "bad"}}])
        )

    def test_no_existing_run_overwrite(self):
        self.run_eval()
        with self.assertRaises(ValueError):
            self.run_eval()

    def test_readiness_only_allows_unknown_destination_without_inference(self):
        self.c["data_destination"] = "unknown"
        self.c["paid"] = None
        with patch.object(
            SyntheticBackend,
            "complete",
            side_effect=AssertionError("Inference prohibited"),
        ):
            d = self.run_eval(readiness_only=True)
        self.assertEqual(len(d["attempts"]), 1)

    def test_qualification_requires_all_gates(self):
        self.c["model"].update(revision="fixed-revision", quantization="Q4_K_M")
        self.c["backend"].update(version="1.0", application=None)
        self.c["settings"].update(
            template_sha256="a" * 64, prefix_cache="disabled", kv_quantization="f16"
        )
        d = self.run_eval()
        self.assertEqual(
            recommendation(d, "extraction")["level"], "repeated task qualification"
        )
        d["attempts"] = [a for a in d["attempts"] if a["stage"] != "api"]
        self.assertEqual(
            recommendation(d, "extraction")["level"], "insufficient evidence"
        )

    def test_nonfinite_metrics_rejected(self):
        d = self.run_eval()
        d["attempts"][0]["metrics"]["wall_seconds"] = float("nan")
        with self.assertRaises(ValueError):
            export(d)

    def test_verifier_negative_controls(self):
        v = self.root / "verify.py"
        v.write_text(data("verify.py"))
        good = 'import re\nimport unicodedata\ndef slugify(text: str) -> str:\n    if not isinstance(text, str):\n        raise TypeError("string required")\n    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()\n    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")\n'
        for source, expected in [
            (good, 0),
            ('def slugify(text):\n    return "wrong"\n', 1),
            ("import os\nos._exit(0)\n", 1),
            ('def slugify(text):\n    return text.lower().replace(" ", "-")\n', 1),
        ]:
            candidate = self.root / "candidate.py"
            candidate.write_text(source)
            p = subprocess.run(
                [sys.executable, "-I", str(v), str(candidate)],
                capture_output=True,
                timeout=3,
            )
            self.assertEqual(p.returncode, expected, p.stdout + p.stderr)

    def test_snapshot_allowlist_is_caller_supplied(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as t:
            item = tarfile.TarInfo("RESULT.json")
            item.size = 2
            t.addfile(item, io.BytesIO(b"{}"))
        # The same archive passes the coding fixture's allowlist and fails another.
        allowed = CODING_FIXTURE.allowed_outputs
        self.assertEqual(snapshot(raw.getvalue(), 4096, allowed)["RESULT.json"], b"{}")
        with self.assertRaises(Failure):
            snapshot(raw.getvalue(), 4096, allowed=("something-else.json",))

    def test_tar_links_traversal_extra_files_rejected(self):
        for name, typ in [
            ("work/../escape", tarfile.REGTYPE),
            ("work/slugify.py", tarfile.SYMTYPE),
            ("work/other.py", tarfile.REGTYPE),
        ]:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as t:
                member = tarfile.TarInfo(name)
                member.type = typ
                member.size = 0
                t.addfile(member)
            with self.assertRaises(Failure):
                snapshot(raw.getvalue(), 4096, CODING_FIXTURE.allowed_outputs)

    def test_process_output_limit_and_cleanup(self):
        with self.assertRaises(Failure):
            bounded([sys.executable, "-c", 'print("x"*100000)'], limit=1024)
        with self.assertRaises(Failure):
            bounded([sys.executable, "-c", "import time;time.sleep(5)"], timeout=0.1)

    def test_worker_cleanup_only_owned_names(self):
        p = PiRunner(self.c)
        p.owned = ["toolkit-worker-synthetic"]
        calls = []
        with patch(
            "poundlock_toolkit.pi.bounded",
            side_effect=lambda cmd, **kw: calls.append(cmd) or (0, b""),
        ):
            p.cleanup_owned()
        self.assertEqual(
            calls, [["docker", "rm", "--force", "toolkit-worker-synthetic"]]
        )
        self.assertEqual(p.cleanup, "passed")

    def test_cleanup_failure_reported(self):
        p = PiRunner(self.c)
        p.owned = ["toolkit-worker-synthetic"]
        with patch("poundlock_toolkit.pi.bounded", return_value=(1, b"private error")):
            p.cleanup_owned()
        self.assertEqual(p.cleanup, "failed")

    def test_missing_isolation_fails_closed(self):
        p = PiRunner(self.c)
        with (
            patch("poundlock_toolkit.pi.bounded", side_effect=FileNotFoundError()),
            self.assertRaises(Failure),
        ):
            p.preflight()

    def test_completion_needs_actual_tool_event_and_terminal(self):
        rows = [
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "python3 -I /fixture/verify.py /work/slugify.py"},
            },
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "stopReason": "stop"}],
            },
        ]
        result = b'{"status":"passed","verifier_exit_code":0}'
        events = b"\n".join(json.dumps(v).encode() for v in rows)
        self.assertTrue(CODING_FIXTURE.completion(events, result))
        self.assertFalse(CODING_FIXTURE.completion(b"", result))
        self.assertFalse(
            CODING_FIXTURE.completion(
                events, b'{"status":"passed","verifier_exit_code":false}'
            )
        )

    def test_schema_rejects_negative_metrics_and_unknown_version(self):
        d = self.run_eval()
        d["schema_version"] = 3
        with self.assertRaises(ValueError):
            validate(d)
        d["schema_version"] = 2
        d["attempts"][0]["metrics"]["wall_seconds"] = -1
        with self.assertRaises(ValueError):
            validate(d)

    def test_memory_pools_never_summed(self):
        self.c["inventory"]["memory"]["gpu_pools"] = [
            {"total_bytes": 16, "available_bytes": 8},
            {"total_bytes": 8, "available_bytes": 4},
        ]
        self.assertNotIn("vram_bytes", plan(self.c)["resource_estimate"])


class WireHandler(BaseHTTPRequestHandler):
    mode = "normal"

    def log_message(self, *_):
        pass

    def send(self, body, content="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(
            json.dumps(
                {
                    "data": [{"id": "example-model"}],
                    "models": [{"name": "example-model"}],
                }
            ).encode()
        )

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.mode == "malformed":
            return self.send(b'{"choices":42}')
        if self.mode == "error":
            return self.send(b'{"error":"SECRET-error"}', status=503)
        content = (
            "OK"
            if payload["messages"][0]["content"] == "Reply exactly OK"
            else json.dumps(EXPECTED)
        )
        if self.path == "/api/chat":
            return self.send(
                json.dumps(
                    {
                        "message": {"content": content},
                        "done": True,
                        "done_reason": "stop",
                        "eval_count": 10,
                        "eval_duration": 1000000000,
                        "prompt_eval_count": 20,
                        "prompt_eval_duration": 2000000000,
                    }
                ).encode()
            )
        if payload.get("stream"):
            events = [
                {"choices": [{"delta": {"reasoning_content": "checking"}}]},
                {"choices": [{"delta": {"content": content}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            if self.mode == "missing_finish":
                events = events[:-1]
            if self.mode == "timings":
                events.append(
                    {
                        "timings": {
                            "predicted_per_second": 87.0,
                            "prompt_per_second": 400.0,
                            "prompt_n": 37000,
                            "predicted_n": 512,
                            "draft_n": 600,
                            "draft_n_accepted": 470,
                        }
                    }
                )
            body = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in events)
                + "data: [DONE]\n\n"
            )
            return self.send(body.encode(), "text/event-stream")
        self.send(
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": content}, "finish_reason": "stop"}
                    ]
                }
            ).encode()
        )


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), WireHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        WireHandler.mode = "normal"
        self.c = fixture_config()
        self.c["endpoint"] = f"http://127.0.0.1:{self.server.server_port}"

    def test_openai_full_workflow_over_http(self):
        with tempfile.TemporaryDirectory() as folder:
            d = evaluate(self.c, Path(folder) / "result.json")
            self.assertTrue(all(a["outcome"] == "pass" for a in d["attempts"]))
            # readiness, warmup, api, then the streamed performance attempt.
            m = d["attempts"][3]["metrics"]
            self.assertIsNotNone(m["first_useful_output_seconds"])
            self.assertIsNone(m["decode_tokens_per_second"])

    def test_second_native_ollama_configuration(self):
        self.c["api"] = "ollama"
        self.c["backend"]["name"] = "ollama"
        with tempfile.TemporaryDirectory() as folder:
            d = evaluate(self.c, Path(folder) / "result.json")
            self.assertTrue(all(a["outcome"] == "pass" for a in d["attempts"]))
            m = d["attempts"][1]["metrics"]
            self.assertEqual(m["decode_tokens_per_second"], 10)
            self.assertEqual(m["prefill_tokens_per_second"], 10)
            self.assertIsNone(m["load_seconds"])

    def test_malformed_response_classified(self):
        WireHandler.mode = "malformed"
        with self.assertRaises(Failure) as f:
            Backend(self.c).complete("x")
        self.assertEqual(f.exception.code, "malformed_response")

    def test_missing_stream_finish_rejected(self):
        WireHandler.mode = "missing_finish"
        with self.assertRaises(Failure) as f:
            Backend(self.c).complete("x", stream=True)
        self.assertEqual(f.exception.outcome, "completion_failure")

    def test_streaming_reads_final_timings_chunk(self):
        """llama.cpp servers new enough to report timings on the last
        streamed chunk: kept verbatim, including speculative-decoding
        counters, never derived from wall-clock rates."""
        WireHandler.mode = "timings"
        result = Backend(self.c).complete("x", stream=True)
        m = result["metrics"]
        self.assertEqual(m["decode_tokens_per_second"], 87.0)
        self.assertEqual(m["prefill_tokens_per_second"], 400.0)
        self.assertEqual(m["prompt_n"], 37000)
        self.assertEqual(m["predicted_n"], 512)
        self.assertEqual(m["draft_n"], 600)
        self.assertEqual(m["draft_n_accepted"], 470)

    def test_report_derives_draft_acceptance_from_streamed_timings(self):
        """The counts come off llama.cpp's final streamed chunk; the rate is
        worked out when the report is written and is never part of the evidence."""
        WireHandler.mode = "timings"
        with tempfile.TemporaryDirectory() as folder:
            d = evaluate(self.c, Path(folder) / "result.json")
        performance = next(a for a in d["attempts"] if a["stage"] == "performance")
        self.assertEqual(performance["metrics"]["draft_n"], 600)
        self.assertNotIn("draft_acceptance", performance["metrics"])
        self.assertIn("| 78.3% (470/600) |", report([d]))

    def test_server_error_sanitized(self):
        WireHandler.mode = "error"
        with self.assertRaises(Failure) as f:
            Backend(self.c).complete("x")
        self.assertEqual(str(f.exception), "http_error")
        self.assertEqual(f.exception.outcome, "infrastructure_failure")

    def test_cli_help_lists_exit_codes(self):
        text = parser().format_help()
        self.assertIn("Exit codes", text)
        self.assertIn(str(EXIT_CONSENT), text)
        self.assertIn("pending consent", text)
        self.assertIn(str(EXIT_OK), text)

    def test_next_useful_test_branches(self):
        self.assertIn(
            "identity/inventory",
            next_useful_test(
                "insufficient evidence",
                ["Exact model/backend/template/cache identity is incomplete."],
            ),
        )
        self.assertIn(
            "gates",
            next_useful_test(
                "insufficient evidence",
                ["A required readiness or compatibility gate did not pass."],
            ),
        )
        self.assertIn("repeated-task", next_useful_test("observed smoke pass", []))
        self.assertIn(
            "re-test the deployed combination",
            next_useful_test("repeated task qualification", []),
        )


class ReasoningEffortTests(unittest.TestCase):
    def test_off_omits_unless_an_explicit_off_value_is_configured(self):
        from poundlock_toolkit.backends import reasoning_effort

        self.assertIsNone(reasoning_effort({"reasoning": "off"}))
        self.assertEqual(
            reasoning_effort({"reasoning": "off", "reasoning_off_effort": "none"}),
            "none",
        )
        self.assertEqual(
            reasoning_effort({"reasoning": "low", "reasoning_off_effort": "none"}),
            "low",
        )


if __name__ == "__main__":
    unittest.main()

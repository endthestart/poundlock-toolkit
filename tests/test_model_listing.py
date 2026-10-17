import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from poundlock_toolkit.backends import Backend
from poundlock_toolkit.cli import EXIT_EVAL_FAILED, main
from poundlock_toolkit.core import Failure, config
from poundlock_toolkit.evaluation import evaluate

ROOT = Path(__file__).resolve().parents[1]

SERVER_ID = "qwen3.8-27b-mtp"


class ModelsHandler(BaseHTTPRequestHandler):
    body = {}
    seen = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.seen.append(("GET", self.path))
        raw = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.seen.append(("POST", self.path))
        self.send_response(500)
        self.end_headers()


class ModelNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ModelsHandler.seen = []
        self.c = config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        self.c["endpoint"] = f"http://127.0.0.1:{self.server.server_port}"
        self.c["model"]["id"] = SERVER_ID
        self.c["limits"].update(total_seconds=30, request_seconds=5)

    def tearDown(self):
        self.tmp.cleanup()

    def listed(self, body, model=SERVER_ID, api="openai"):
        ModelsHandler.body = body
        self.c["model"]["id"] = model
        self.c["api"] = api
        if api == "ollama":
            self.c["backend"]["name"] = "ollama"

    def failure(self):
        with self.assertRaises(Failure) as caught:
            Backend(self.c).readiness()
        return caught.exception

    def assertNotListed(self):
        failure = self.failure()
        self.assertEqual(
            (failure.outcome, failure.code), ("protocol_failure", "model_not_listed")
        )

    def test_an_unlisted_model_fails_and_a_listed_one_passes(self):
        self.listed({"data": [{"id": "something-else"}, {"id": "third"}]})
        self.assertNotListed()
        self.listed({"data": [{"id": SERVER_ID}]})
        self.assertIsNotNone(Backend(self.c).readiness()["response_sha256"])

    def test_ollama_lists_by_name(self):
        self.listed({"models": [{"name": "something-else"}]}, api="ollama")
        self.assertNotListed()
        self.listed({"models": [{"name": SERVER_ID}]}, api="ollama")
        self.assertIsNotNone(Backend(self.c).readiness()["response_sha256"])

    def test_the_run_fails_at_readiness_and_substitutes_nothing(self):
        self.listed({"data": [{"id": "something-else"}]})
        d = evaluate(self.c, self.root / "result.json")
        readiness, *rest = d["attempts"]
        self.assertEqual(
            (readiness["outcome"], readiness["code"]),
            ("protocol_failure", "model_not_listed"),
        )
        self.assertTrue(rest)
        self.assertTrue(all(a["outcome"] == "skipped" for a in rest))
        # Only the model list was ever requested: no inference with any name.
        self.assertEqual(ModelsHandler.seen, [("GET", "/models")])
        self.assertEqual(self.c["model"]["id"], SERVER_ID)

    def test_the_cli_exits_with_a_failure(self):
        self.listed({"data": [{"id": "something-else"}]})
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps(self.c))
        argv = ["--readiness-only", "--output"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["plan", str(cfg), *argv, str(self.root / "plan.json")]), 0
            )
            code = main(
                [
                    "evaluate",
                    str(cfg),
                    "--plan",
                    str(self.root / "plan.json"),
                    "--execute",
                    *argv,
                    str(self.root / "run.json"),
                ]
            )
        self.assertEqual(code, EXIT_EVAL_FAILED)
        self.assertEqual(ModelsHandler.seen, [("GET", "/models")])


if __name__ == "__main__":
    unittest.main()

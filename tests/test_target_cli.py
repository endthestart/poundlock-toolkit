from argparse import Namespace
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poundlock_toolkit import target_cli

ROOT = Path(__file__).resolve().parents[1]
TOOL_SCHEMA = (
    ROOT / "src" / "poundlock_toolkit" / "data" / "examples" / "tool-schema.json"
)
OBSERVED = {
    "inventory": {"os": "linux", "architecture": "x86_64", "memory": {"total_bytes": 1}}
}


class TargetConfigLoopbackTests(unittest.TestCase):
    """target-config adds observed inventory only to a config whose endpoints are the target's own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_config(self, endpoint, tool_schema_endpoint):
        document = json.loads(TOOL_SCHEMA.read_text(encoding="utf-8"))
        document["endpoint"] = endpoint
        document["tool_schema"]["endpoints"]["openai"] = tool_schema_endpoint
        path = self.root / "config.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        output = self.root / "observed.json"
        output.unlink(missing_ok=True)
        args = Namespace(
            command="target-config",
            target="target.json",
            config=str(path),
            output=str(output),
        )
        with (
            patch.object(
                target_cli, "target_config", return_value={"transport": "local"}
            ),
            patch.object(target_cli, "inventory", return_value=OBSERVED) as observed,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                target_cli.run(args)
            except ValueError:
                return None, observed.called
        return json.loads(output.read_text(encoding="utf-8")), observed.called

    def test_loopback_endpoints_get_observed_inventory(self):
        for host in ("127.0.0.1", "[::1]", "localhost"):
            result, observed = self.run_config(
                f"http://{host}:8080/v1", f"http://{host}:8080/v1"
            )
            self.assertTrue(observed, host)
            self.assertEqual(result["inventory"]["source"], "observed", host)

    def test_a_network_endpoint_is_refused_before_inventory(self):
        for endpoint, tool_schema in (
            ("http://10.20.0.5:8080/v1", "http://127.0.0.1:8080/v1"),
            ("http://127.0.0.1:8080/v1", "http://10.20.0.5:8080/v1"),
        ):
            result, observed = self.run_config(endpoint, tool_schema)
            self.assertIsNone(result, endpoint)
            self.assertFalse(observed, endpoint)


if __name__ == "__main__":
    unittest.main()

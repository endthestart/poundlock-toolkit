from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from poundlock_toolkit.core import config, digest, read_json
from poundlock_toolkit.inspection import drm_inventory
from poundlock_toolkit.targets import (
    command,
    control,
    exchange,
    implementation_id,
    inventory,
    start,
    target_config,
    target_plan,
    TransportError,
)
from poundlock_toolkit.setup import setup_plan, setup_script, setup_spec
from test_toolkit import WireHandler

ROOT = Path(__file__).resolve().parents[1]


def require_isolated_install():
    """Local targets run `python -I`, which ignores PYTHONPATH by design.

    The interpreter must import this checkout's package on its own; a stale
    editable install (for example after moving the checkout) otherwise surfaces
    only as an opaque TransportError from every target test.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import poundlock_toolkit; print(poundlock_toolkit.__file__)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    expected = ROOT / "src" / "poundlock_toolkit" / "__init__.py"
    found = probe.stdout.strip()
    if probe.returncode != 0 or Path(found).resolve() != expected:
        raise RuntimeError(
            f"{sys.executable} -I imports {found or 'no poundlock_toolkit'}; "
            f"reinstall this checkout (pip install -e {ROOT}) before target tests"
        )


class TargetTests(unittest.TestCase):
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

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_inventory_is_target_side_and_read_only(self):
        before = set(self.root.iterdir())
        observed = inventory(self.target)
        self.assertEqual(observed["implementation"], implementation_id())
        self.assertIn("memory", observed["inventory"])
        self.assertEqual(before, set(self.root.iterdir()))

    def test_strict_ssh_quote_host_and_executable(self):
        target = {
            "transport": "ssh",
            "host": "test-host",
            "python": "/opt/a b/python'$(touch nope)",
        }
        p = self.root / "target.json"
        p.write_text(json.dumps(target))
        target_config(p)
        cmd = command(target)
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn("ForwardAgent=no", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(shlex.split(cmd[-1])[0], target["python"])
        target["host"] = "-oProxyCommand=bad"
        p.write_text(json.dumps(target))
        with self.assertRaises(ValueError):
            target_config(p)

    def test_package_mismatch_fails_before_writes(self):
        with self.assertRaises(TransportError):
            exchange(self.target, {"operation": "inspect", "implementation": "0" * 64})
        self.assertFalse((self.root / ".local").exists())

    def test_lifecycle_transfer_checksum_and_no_double_run(self):
        WireHandler.mode = "normal"
        server = ThreadingHTTPServer(("127.0.0.1", 0), WireHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.c["endpoint"] = f"http://127.0.0.1:{server.server_port}"
        try:
            plan = target_plan(self.c, self.target)
            receipt = self.root / "receipt.json"
            result = start(self.c, self.target, plan, receipt)
            self.assertEqual(result["state"]["status"], "completed")
            self.assertEqual(result["result_sha256"], digest(result["result"]))
            saved = read_json(receipt)
            again = control(self.target, saved, "status")
            self.assertEqual(result["result_sha256"], again["result_sha256"])
            with self.assertRaises(TransportError):
                exchange(
                    self.target, dict(saved, operation="run", config=self.c, plan=plan)
                )
            denied = dict(saved, token="0" * 32)
            with self.assertRaises(TransportError):
                control(self.target, denied, "remove")
            self.assertEqual(
                control(self.target, saved, "remove")["state"]["status"], "removed"
            )
            self.assertFalse(
                (
                    self.root / ".local/state/poundlock-toolkit/runs" / saved["run_id"]
                ).exists()
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_disconnect_cancels_and_status_reconnect_does_not_restart(self):
        plan = target_plan(self.c, self.target, True)
        packet = {
            "protocol": 1,
            "implementation": implementation_id(),
            "operation": "run",
            "run_id": str(uuid.uuid4()),
            "token": uuid.uuid4().hex,
            "config": self.c,
            "plan": plan,
        }

        # A stalled readiness connection leaves time to observe transport loss.
        class Slow(WireHandler):
            def do_GET(s):
                time.sleep(3)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.c["endpoint"] = f"http://127.0.0.1:{server.server_port}"
        plan = target_plan(self.c, self.target, True)
        packet.update(plan=plan, plan_sha256=digest(plan), config_sha256=digest(self.c))
        p = subprocess.Popen(
            command(self.target),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            p.stdin.write(json.dumps(packet).encode() + b"\n")
            p.stdin.flush()
            owned = self.root / ".local/state/poundlock-toolkit/runs" / packet["run_id"]
            end = time.monotonic() + 3
            while not (owned / "state.json").exists() and time.monotonic() < end:
                time.sleep(0.02)
            p.stdin.close()
            p.wait(timeout=6)
            receipt = {
                k: packet[k]
                for k in (
                    "protocol",
                    "implementation",
                    "run_id",
                    "token",
                    "plan_sha256",
                    "config_sha256",
                )
            }
            receipt["target_sha256"] = digest(self.target)
            result = control(self.target, receipt, "status")
            self.assertIn(result["state"]["status"], ("cancelled", "interrupted"))
            again = control(self.target, receipt, "status")
            self.assertEqual(result, again)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
            p.stderr.close()
            server.shutdown()
            server.server_close()
            t.join()

    def test_receipt_wrong_target_refused(self):
        with self.assertRaises(ValueError):
            control(self.target, {"target_sha256": "0" * 64}, "status")

    def test_symlink_state_directory_rejected(self):
        (self.root / "outside").mkdir()
        (self.root / ".local").symlink_to(
            self.root / "outside", target_is_directory=True
        )
        self.c["endpoint"] = "http://127.0.0.1:1/v1"
        plan = target_plan(self.c, self.target, True)
        with self.assertRaises(TransportError):
            start(self.c, self.target, plan, self.root / "receipt.json")
        self.assertEqual(list((self.root / "outside").iterdir()), [])

    def test_target_plan_change_refused_before_start(self):
        plan = target_plan(self.c, self.target)
        self.c["settings"]["max_output_tokens"] += 1
        with self.assertRaises(ValueError):
            start(self.c, self.target, plan, self.root / "receipt.json")
        self.assertFalse((self.root / "receipt.json").exists())

    def test_transport_write_deadline_includes_stalled_input(self):
        # A selected target process that never consumes stdin cannot trap the client.
        target_command = [sys.executable, "-c", "import time; time.sleep(20)"]
        started = time.monotonic()
        with (
            patch("poundlock_toolkit.targets.command", return_value=target_command),
            self.assertRaises(TransportError),
        ):
            exchange(self.target, {"padding": "x" * 200000}, timeout=0.2)
        self.assertLess(time.monotonic() - started, 4)

    def test_cancel_running_run_and_refuse_active_removal(self):
        class Slow(WireHandler):
            def do_GET(s):
                time.sleep(3)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.c["endpoint"] = f"http://127.0.0.1:{server.server_port}"
        plan = target_plan(self.c, self.target, True)
        packet = {
            "protocol": 1,
            "implementation": implementation_id(),
            "operation": "run",
            "run_id": str(uuid.uuid4()),
            "token": uuid.uuid4().hex,
            "config": self.c,
            "plan": plan,
            "plan_sha256": digest(plan),
            "config_sha256": digest(self.c),
        }
        receipt = {
            k: packet[k]
            for k in (
                "protocol",
                "implementation",
                "run_id",
                "token",
                "plan_sha256",
                "config_sha256",
            )
        }
        receipt["target_sha256"] = digest(self.target)
        p = subprocess.Popen(
            command(self.target),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            p.stdin.write(json.dumps(packet).encode() + b"\n")
            p.stdin.flush()
            owned = self.root / ".local/state/poundlock-toolkit/runs" / packet["run_id"]
            end = time.monotonic() + 3
            while not (owned / "state.json").exists() and time.monotonic() < end:
                time.sleep(0.02)
            with self.assertRaises(TransportError):
                control(self.target, receipt, "remove")
            control(self.target, receipt, "cancel")
            p.wait(timeout=5)
            result = control(self.target, receipt, "status")
            self.assertIn(result["state"]["status"], ("cancelled", "interrupted"))
            (owned / "pending.owned.json").write_text('{"cleanup":"pending"}')
            with self.assertRaises(TransportError):
                control(self.target, receipt, "remove")
            self.assertTrue(owned.exists())
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()
            server.shutdown()
            server.server_close()
            thread.join()


class SetupInventoryTests(unittest.TestCase):
    def test_amd_pool_and_intel_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n, vendor in [(0, "0x1002"), (1, "0x8086")]:
                p = root / f"card{n}" / "device"
                p.mkdir(parents=True)
                (p / "vendor").write_text(vendor)
                (p / "device").write_text("0x1234")
            (root / "card0/device/mem_info_vram_total").write_text("16384")
            (root / "card0/device/mem_info_vram_used").write_text("4096")
            result = drm_inventory(root)
            self.assertEqual(result[0]["available_bytes"], 12288)
            self.assertIsNone(result[1]["total_bytes"])

    def test_setup_script_quotes_literals_and_is_finite(self):
        spec = setup_spec(ROOT / "src/poundlock_toolkit/data/examples/standalone.json")
        spec["model_path"] = "/models/a '$(touch SHOULD_NOT_EXIST).gguf"
        plan = setup_plan(spec)
        script = setup_script(spec, plan)
        self.assertIn("--internal", script)
        self.assertIn("127.0.0.1:18080:8080", script)
        self.assertIn("--pull=never", script)
        self.assertIn("/usr/bin/timeout", script)
        self.assertIn("server.cid", script)
        self.assertIn("setup-cleanup.json", script)
        self.assertEqual(
            subprocess.run(
                ["sh", "-n"], input=script, text=True, capture_output=True
            ).returncode,
            0,
        )
        self.assertIn(shlex.quote(spec["model_path"]), script)

    def test_setup_plan_binding_and_privilege_limits(self):
        spec = setup_spec(ROOT / "src/poundlock_toolkit/data/examples/standalone.json")
        plan = setup_plan(spec)
        self.assertEqual(plan["downloads_bytes"], 0)
        self.assertEqual(plan["privileged_steps"], [])
        spec["cpus"] = 4
        with self.assertRaises(ValueError):
            setup_script(spec, plan)

    def test_setup_accepts_dispatch_pi_version_and_rejects_unknown_versions(self):
        raw = json.loads(
            (ROOT / "src/poundlock_toolkit/data/examples/standalone.json").read_text()
        )
        self.assertEqual(
            setup_spec(ROOT / "src/poundlock_toolkit/data/examples/standalone.json")[
                "pi_version"
            ],
            "1.1.0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "standalone.json"
            # A version never reviewed, and one retired in 0.5.0.
            for version in ("0.86.0", "0.85.0"):
                raw["pi_version"] = version
                path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "outside reviewed scope"):
                    setup_spec(path)

    def test_setup_displayed_scope_is_bound(self):
        spec = setup_spec(ROOT / "src/poundlock_toolkit/data/examples/standalone.json")
        plan = setup_plan(spec)
        plan["scope"]["server_cpus"] = 1
        with self.assertRaises(ValueError):
            setup_script(spec, plan)

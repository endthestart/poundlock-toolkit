"""Sampling a local server process's memory, and what the result may claim."""

import copy
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from poundlock_toolkit.cache_probe import PROBE_REQUESTS
from poundlock_toolkit.core import config, digest, read_json, validate
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.drift import evaluation_check
from poundlock_toolkit.evaluation import evaluate
from poundlock_toolkit.memory_probe import (
    DarwinReader,
    LinuxReader,
    MemorySampler,
    ProcessGone,
    Unreadable,
    clean_name,
    find_pids,
    open_reader,
    open_sampler,
)
from poundlock_toolkit.planning import plan
from poundlock_toolkit.reporting import memory_text, report
from poundlock_toolkit.targets import start, target_plan
from test_targets import require_isolated_install
from test_toolkit import WireHandler

ROOT = Path(__file__).resolve().parents[1]
GIB = 2**30


def with_probe(c, **spec):
    c["memory_probe"] = spec or {"pid": 4242}
    c["endpoint"] = "http://127.0.0.1:8080/v1"
    return c


class FakeProc:
    """A fake /proc and /sys/class/drm tree."""

    def __init__(self, root):
        self.proc, self.drm = Path(root) / "proc", Path(root) / "drm"
        self.proc.mkdir()
        self.drm.mkdir()

    def process(self, pid, rss_kb=2048, comm="llama-server", start="9000", state="S"):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "status").write_text(
            f"Name:\t{comm}\nState:\t{state} (sleeping)\nVmHWM:\t{rss_kb + 5} kB\n"
            f"VmRSS:\t{rss_kb} kB\nThreads:\t4\n"
        )
        # Field 2 (the command) may hold spaces and parentheses.
        (d / "stat").write_text(
            f"{pid} ({comm} (odd) name) {state} 1 1 1 0 -1 4194560 100 0 0 0 5 5 0 0 "
            f"20 0 4 0 {start} 1000000 500 18446744073709551615\n"
        )
        (d / "comm").write_text(comm + "\n")

    def card(
        self,
        name,
        vendor="0x1002",
        pci="0x744c",
        used="1073741824",
        total="17163091968",
    ):
        device = self.drm / name / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        (device / "device").write_text(pci + "\n")
        if used is not None:
            (device / "mem_info_vram_used").write_text(used + "\n")
        if total is not None:
            (device / "mem_info_vram_total").write_text(total + "\n")

    def reader(self, pid):
        return LinuxReader(pid, self.proc, self.drm)


class ScriptedReader:
    """Yields the given samples in order; then raises what `after` says."""

    source = "linux-procfs"

    def __init__(self, samples, after=ProcessGone):
        self.samples, self.after, self.calls = list(samples), after, 0

    def name(self):
        return "llama-server"

    def read(self):
        self.calls += 1
        if not self.samples:
            raise self.after()
        value = self.samples.pop(0)
        return {"rss_bytes": value, "vram": []} if isinstance(value, int) else value


class LinuxReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeProc(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_resident_set_from_procfs_status(self):
        self.fake.process(100, rss_kb=2048)
        sample = self.fake.reader(100).read()
        self.assertEqual(sample["rss_bytes"], 2048 * 1024)
        self.assertEqual(sample["vram"], [])

    def test_reads_device_wide_vram_per_amd_card_and_never_sums(self):
        self.fake.process(100)
        self.fake.card("card0", used="1000", total="16000")
        self.fake.card("card1", used="3000", total="8000", pci="0x1002")
        pools = self.fake.reader(100).read()["vram"]
        self.assertEqual(
            [(p["drm_card"], p["used_bytes"], p["total_bytes"]) for p in pools],
            [(0, 1000, 16000), (1, 3000, 8000)],
        )
        self.assertEqual(len(pools), 2)

    def test_only_amd_cards_with_a_used_counter_are_sampled(self):
        self.fake.process(100)
        self.fake.card("card0", vendor="0x10de")  # not AMD
        self.fake.card("card1", used=None)  # no counter exposed
        self.fake.card("card2", used="not-a-number")
        self.fake.card("card3", used="500")
        (self.fake.drm / "card3-DP-1").mkdir()  # a connector, not a card
        pools = self.fake.reader(100).read()["vram"]
        self.assertEqual([p["drm_card"] for p in pools], [3])

    def test_a_missing_process_is_gone_not_zero(self):
        with self.assertRaises(ProcessGone):
            self.fake.reader(999).read()

    def test_a_zombie_is_gone(self):
        self.fake.process(100, state="Z")
        with self.assertRaises(ProcessGone):
            self.fake.reader(100).read()

    def test_a_reused_pid_is_gone_not_the_same_server(self):
        self.fake.process(100, start="9000")
        reader = self.fake.reader(100)
        reader.read()
        self.fake.process(100, start="9500", rss_kb=999999)
        with self.assertRaises(ProcessGone):
            reader.read()

    def test_a_process_without_a_resident_set_is_unreadable(self):
        self.fake.process(100)
        (self.fake.proc / "100" / "status").write_text("Name:\tkthreadd\nState:\tS\n")
        with self.assertRaises(Unreadable):
            self.fake.reader(100).read()

    def test_the_name_is_a_short_safe_string_or_none(self):
        self.fake.process(100, comm="llama-server")
        self.assertEqual(self.fake.reader(100).name(), "llama-server")
        self.fake.process(101, comm="my server/../x")
        self.assertIsNone(self.fake.reader(101).name())
        self.assertIsNone(clean_name("a" * 65))


class DarwinReaderTests(unittest.TestCase):
    def ps(self, rows):
        calls = []

        def run(args):
            calls.append(args)
            return rows.pop(0) if rows else ""

        run.calls = calls
        return run

    def test_reads_resident_set_in_kib(self):
        ps = self.ps(["  204800 Tue Sep 29 10:00:00 2026"])
        sample = DarwinReader(4242, ps).read()
        self.assertEqual(sample["rss_bytes"], 204800 * 1024)
        # No VRAM on macOS: unified memory is not established by RSS.
        self.assertEqual(sample["vram"], [])
        self.assertEqual(ps.calls[0], ["-o", "rss=,lstart=", "-p", "4242"])

    def test_missing_process_and_reused_pid_are_gone(self):
        reader = DarwinReader(
            4242,
            self.ps(
                [
                    "100 Tue Sep 29 10:00:00 2026",
                    "100 Tue Sep 29 11:30:00 2026",
                    "",
                ]
            ),
        )
        reader.read()
        with self.assertRaises(ProcessGone):
            reader.read()
        with self.assertRaises(ProcessGone):
            DarwinReader(4242, self.ps([""])).read()

    def test_garbage_is_unreadable(self):
        with self.assertRaises(Unreadable):
            DarwinReader(1, self.ps(["abc def"])).read()

    def test_name_is_the_executable_basename(self):
        reader = DarwinReader(1, self.ps(["/opt/homebrew/bin/llama-server"]))
        self.assertEqual(reader.name(), "llama-server")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeProc(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, name, **kw):
        return open_reader(
            {"process": name}, "Linux", self.fake.proc, self.fake.drm, **kw
        )

    def test_a_process_name_must_match_exactly_one_process(self):
        self.fake.process(10, comm="llama-server")
        self.fake.process(11, comm="llama-serverx")
        self.fake.process(12, comm="python3")
        reader, reason = self.open("llama-server")
        self.assertIsNone(reason)
        self.assertEqual(reader.pid, 10)

    def test_two_matches_is_ambiguous_never_a_guess(self):
        self.fake.process(10, comm="llama-server")
        self.fake.process(11, comm="llama-server")
        self.assertEqual(self.open("llama-server"), (None, "process_ambiguous"))

    def test_no_match_is_not_found(self):
        self.fake.process(12, comm="python3")
        self.assertEqual(self.open("llama-server"), (None, "process_not_found"))

    def test_the_kernels_15_character_command_limit_is_allowed_for(self):
        self.fake.process(10, comm="a-very-long-exe"[:15])
        reader, _ = self.open("a-very-long-executable")
        self.assertEqual(reader.pid, 10)

    def test_a_named_pid_is_checked_before_the_run(self):
        self.fake.process(10)
        reader, reason = open_reader(
            {"pid": 10}, "Linux", self.fake.proc, self.fake.drm
        )
        self.assertEqual((reader.pid, reason), (10, None))
        self.assertEqual(
            open_reader({"pid": 77}, "Linux", self.fake.proc, self.fake.drm),
            (None, "process_not_found"),
        )
        (self.fake.proc / "10" / "status").write_text("Name:\tx\nState:\tS\n")
        self.assertEqual(
            open_reader({"pid": 10}, "Linux", self.fake.proc, self.fake.drm),
            (None, "unreadable"),
        )

    def test_other_platforms_are_unsupported_not_guessed(self):
        for system in ("Windows", "FreeBSD", "Java"):
            self.assertEqual(
                open_reader({"pid": 1}, system), (None, "unsupported_platform")
            )

    def test_darwin_name_lookup_uses_the_executable_basename(self):
        ps_output = (
            f"  {os.getpid()} /usr/bin/python3\n"
            "  501 /opt/homebrew/bin/llama-server\n"
            "  502 /Applications/Some App/Contents/MacOS/other\n"
        )
        found = find_pids("llama-server", "Darwin", ps=lambda args: ps_output)
        self.assertEqual(found, [501])
        self.assertEqual(find_pids("python3", "Darwin", ps=lambda args: ps_output), [])


@unittest.skipUnless(platform.system() in ("Linux", "Darwin"), "Linux and macOS only")
class RealProcessTests(unittest.TestCase):
    """The real /proc or `ps` against a process this test owns."""

    def test_reads_this_process_on_this_platform(self):
        reader, reason = open_reader({"pid": os.getpid()})
        self.assertIsNone(reason)
        sample = reader.read()
        self.assertGreater(sample["rss_bytes"], 1_000_000)
        self.assertLess(sample["rss_bytes"], 2**40)
        self.assertEqual(
            reader.source,
            "linux-procfs" if platform.system() == "Linux" else "macos-ps",
        )

    def test_an_exited_child_is_gone(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            reader, reason = open_reader({"pid": child.pid})
            self.assertIsNone(reason)
            self.assertGreater(reader.read()["rss_bytes"], 0)
        finally:
            child.kill()
            child.wait()
        with self.assertRaises(ProcessGone):
            reader.read()


class SamplerTests(unittest.TestCase):
    def test_window_peak_covers_only_samples_inside_the_window(self):
        clock = iter(range(1000)).__next__
        sampler = MemorySampler(ScriptedReader([900, 300, 200, 50, 60]), "s", 1, clock)
        sampler.sample_now()  # 900: taken before the first window opens
        start = sampler.mark()  # samples 300
        self.assertEqual(sampler.peak_since(start), 300)  # then samples 200
        # A later window sees none of the earlier samples, only its own two.
        start = sampler.mark()  # samples 50
        self.assertEqual(sampler.peak_since(start), 60)

    def test_summary_reports_first_and_highest_sampled_and_the_count(self):
        sampler = MemorySampler(ScriptedReader([10, 40, 20]), "llama-server", 0.5)
        for _ in range(3):
            sampler.sample_now()
        s = sampler.summary()
        self.assertEqual(
            (
                s["status"],
                s["samples"],
                s["first_process_rss_bytes"],
                s["peak_process_rss_bytes"],
            ),
            ("sampled", 3, 10, 40),
        )
        self.assertEqual(
            (s["process_name"], s["interval_seconds"]), ("llama-server", 0.5)
        )
        self.assertFalse(s["process_exited"])

    def test_gpu_pools_stay_separate_and_track_first_and_peak_per_card(self):
        def pools(a, b):
            return {
                "rss_bytes": 1,
                "vram": [
                    {
                        "drm_card": 0,
                        "pci_device_id": "0x744c",
                        "total_bytes": 16,
                        "used_bytes": a,
                    },
                    {
                        "drm_card": 1,
                        "pci_device_id": None,
                        "total_bytes": 8,
                        "used_bytes": b,
                    },
                ],
            }

        sampler = MemorySampler(ScriptedReader([pools(5, 1), pools(9, 2), pools(7, 3)]))
        for _ in range(3):
            sampler.sample_now()
        rows = sampler.summary()["gpu_pools"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            (rows[0]["first_used_bytes"], rows[0]["peak_used_bytes"]), (5, 9)
        )
        self.assertEqual(
            (rows[1]["first_used_bytes"], rows[1]["peak_used_bytes"]), (1, 3)
        )
        self.assertTrue(all(r["scope"] == "device_wide" for r in rows))

    def test_a_vanished_process_is_recorded_and_sampling_stops(self):
        reader = ScriptedReader([100, 200])
        sampler = MemorySampler(reader)
        for _ in range(5):
            sampler.sample_now()
        self.assertTrue(sampler.exited)
        self.assertEqual(sampler.summary()["samples"], 2)
        self.assertTrue(sampler.summary()["process_exited"])
        self.assertEqual(reader.calls, 3)  # not read again once it was gone

    def test_unreadable_samples_leave_the_peak_null_never_zero(self):
        sampler = MemorySampler(ScriptedReader([], after=Unreadable))
        start = sampler.mark()
        self.assertIsNone(sampler.peak_since(start))
        s = sampler.summary()
        self.assertEqual(
            (s["status"], s["reason"], s["peak_process_rss_bytes"]),
            ("unavailable", "unreadable", None),
        )
        self.assertEqual(s["gpu_pools"], [])

    def test_the_background_thread_samples_between_boundaries(self):
        sampler = MemorySampler(ScriptedReader(list(range(1, 200))), "s", 0.005)
        sampler.start()
        deadline = time.monotonic() + 3
        while len(sampler.samples) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        sampler.stop()
        self.assertGreaterEqual(len(sampler.samples), 5)
        self.assertFalse(sampler.thread.is_alive())

    def test_open_sampler_names_the_process_or_reports_why_not(self):
        sampler, gap = open_sampler({"pid": 1}, ScriptedReader([1]))
        self.assertEqual((sampler.name, gap), ("llama-server", None))
        sampler, gap = open_sampler({"process": "no-such-server-8f3a1c"})
        self.assertIsNone(sampler)
        self.assertEqual(
            (gap["status"], gap["reason"], gap["peak_process_rss_bytes"]),
            ("unavailable", "process_not_found", None),
        )


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")

    def test_off_by_default_and_the_plan_is_unchanged(self):
        p = plan(self.c)
        self.assertNotIn("memory_probe", p)
        self.assertNotIn("memory_probe", p["reviewed_inputs"])

    def test_the_probe_is_in_the_plan_as_a_zero_request_unprivileged_read(self):
        with_probe(self.c, process="llama-server")
        p = plan(self.c)
        self.assertEqual(p["memory_probe"]["target"], {"process": "llama-server"})
        self.assertEqual(p["memory_probe"]["requests"], 0)
        self.assertFalse(p["memory_probe"]["privileged"])
        self.assertEqual(p["download_bytes"], 0)
        self.assertEqual(p["maximum_direct_requests"], 7)
        self.assertEqual(p["blockers"], [])
        self.assertIn("Highest sampled value", p["memory_probe"]["note"])
        self.assertIn("memory_probe", p["reviewed_inputs"])

    def test_adding_or_changing_the_probe_after_review_is_drift(self):
        reviewed = plan(self.c)
        with_probe(self.c, pid=10)
        added = evaluation_check(reviewed, plan(self.c))
        self.assertEqual(added["drifted"], ["memory_probe"])
        reviewed = plan(self.c)
        self.c["memory_probe"] = {"pid": 11}
        self.assertEqual(
            evaluation_check(reviewed, plan(self.c))["drifted"], ["memory_probe"]
        )

    def test_a_remote_endpoint_blocks_it_rather_than_reporting_a_number(self):
        for endpoint in (
            "http://198.51.100.7:8080/v1",
            "https://inference.example.org/v1",
            "http://[2001:db8::1]:8080/v1",
        ):
            with self.subTest(endpoint=endpoint):
                c = copy.deepcopy(self.c)
                c["memory_probe"] = {"pid": 10}
                c["endpoint"] = endpoint
                blockers = plan(c)["blockers"]
                self.assertTrue(any("loopback" in b for b in blockers))
                with tempfile.TemporaryDirectory() as folder:
                    with self.assertRaises(ValueError):
                        evaluate(c, Path(folder) / "r.json", backend=SyntheticBackend())
                    self.assertEqual(list(Path(folder).iterdir()), [])

    def test_loopback_spellings_are_allowed(self):
        for endpoint in (
            "http://127.0.0.1:1/v1",
            "http://localhost:1/v1",
            "http://[::1]:1/v1",
        ):
            with self.subTest(endpoint=endpoint):
                c = copy.deepcopy(self.c)
                c["memory_probe"] = {"pid": 10}
                c["endpoint"] = endpoint
                self.assertEqual(plan(c)["blockers"], [])

    def test_readiness_only_never_samples(self):
        with_probe(self.c)
        p = plan(self.c, readiness_only=True)
        self.assertNotIn("memory_probe", p)

    def test_schema_takes_exactly_one_of_pid_or_process(self):
        for spec, ok in (
            ({"pid": 5}, True),
            ({"process": "llama-server"}, True),
            ({}, False),
            ({"pid": 5, "process": "x"}, False),
            ({"pid": 0}, False),
            ({"pid": "5"}, False),
            ({"process": "a b"}, False),
            ({"process": "../x"}, False),
            ({"pid": 5, "extra": 1}, False),
        ):
            with self.subTest(spec=spec):
                c = copy.deepcopy(self.c)
                c["memory_probe"] = spec
                if ok:
                    validate(c, "config.schema.json")
                else:
                    with self.assertRaises(ValueError):
                        validate(c, "config.schema.json")

    def test_a_tool_schema_scenario_cannot_carry_it(self):
        c = config(ROOT / "src/poundlock_toolkit/data/examples/tool-schema.json")
        c["memory_probe"] = {"pid": 5}
        with self.assertRaises(ValueError):
            validate(c, "config.schema.json")


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = with_probe(
            config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_eval(self, samples, name="r.json", **kw):
        return evaluate(
            self.c,
            self.root / name,
            backend=SyntheticBackend(),
            memory_reader=ScriptedReader(samples, after=Unreadable),
            # The thread never fires: every sample here is a boundary sample.
            memory_interval=1000,
            **kw,
        )

    def test_each_attempt_records_the_highest_resident_set_sampled_during_it(self):
        # One sample when sampling starts, then one at the start and one at the end
        # of each of the seven attempts; the first attempt sees a spike.
        pairs = [(11, 50), (12, 12), (13, 13), (14, 14), (15, 15), (16, 16), (17, 17)]
        d = self.run_eval([10] + [value for pair in pairs for value in pair])
        peaks = [a["metrics"]["peak_process_rss_bytes"] for a in d["attempts"]]
        self.assertEqual(peaks, [50, 12, 13, 14, 15, 16, 17])
        block = d["memory_probe"]
        self.assertEqual(block["status"], "sampled")
        self.assertEqual(block["samples"], 15)
        self.assertEqual(block["first_process_rss_bytes"], 10)
        self.assertEqual(block["peak_process_rss_bytes"], 50)
        self.assertEqual(block["process_name"], "llama-server")
        self.assertEqual(block["source"], "linux-procfs")
        self.assertEqual(block["interval_seconds"], 1000)
        self.assertFalse(block["process_exited"])
        validate(d)

    def test_a_sample_that_cannot_be_read_leaves_the_metric_null(self):
        d = self.run_eval([])
        self.assertTrue(
            all(a["metrics"]["peak_process_rss_bytes"] is None for a in d["attempts"])
        )
        self.assertEqual(d["memory_probe"]["status"], "unavailable")
        self.assertEqual(d["memory_probe"]["reason"], "unreadable")
        self.assertIsNone(d["memory_probe"]["peak_process_rss_bytes"])
        self.assertIn("peak memory is unknown", memory_text(d))
        self.assertTrue(all(a["outcome"] == "pass" for a in d["attempts"]))

    def test_sampling_never_changes_what_is_scored(self):
        with_memory = self.run_eval([1] * 100)
        without = copy.deepcopy(self.c)
        del without["memory_probe"]
        plain = evaluate(without, self.root / "plain.json", backend=SyntheticBackend())

        def shape(doc):
            return [(a["stage"], a["workload"], a["outcome"]) for a in doc["attempts"]]

        self.assertEqual(shape(with_memory), shape(plain))
        self.assertNotIn("memory_probe", plain)
        self.assertTrue(
            all(
                a["metrics"]["peak_process_rss_bytes"] is None
                for a in plain["attempts"]
            )
        )

    def test_process_exit_is_flagged(self):
        d = evaluate(
            self.c,
            self.root / "gone.json",
            backend=SyntheticBackend(),
            memory_reader=ScriptedReader([100, 100, 100], after=ProcessGone),
            memory_interval=1000,
        )
        self.assertTrue(d["memory_probe"]["process_exited"])
        self.assertIn("ended, or its PID was reused", report([d]))
        self.assertTrue(all(a["outcome"] == "pass" for a in d["attempts"]))

    def test_gpu_pools_are_reported_per_card_and_labelled_device_wide(self):
        def pool(card, used):
            return {
                "drm_card": card,
                "pci_device_id": "0x744c",
                "total_bytes": 16 * GIB,
                "used_bytes": used,
            }

        samples = [
            {"rss_bytes": 9 * GIB, "vram": [pool(0, 6 * GIB), pool(1, GIB)]},
            {"rss_bytes": 10 * GIB, "vram": [pool(0, 14 * GIB), pool(1, 2 * GIB)]},
        ] * 30
        d = self.run_eval(samples)
        pools = d["memory_probe"]["gpu_pools"]
        self.assertEqual([p["drm_card"] for p in pools], [0, 1])
        self.assertEqual(pools[0]["peak_used_bytes"], 14 * GIB)
        self.assertEqual(pools[1]["peak_used_bytes"], 2 * GIB)
        text = memory_text(d)
        self.assertIn("device-wide", text)
        self.assertIn("never summed", text)
        self.assertIn("every process on the card", text)
        self.assertNotIn("16.00 GiB total", text)
        validate(d)

    def test_report_says_what_the_number_is_and_is_not(self):
        d = self.run_eval([3 * GIB] * 50)
        text = report([d])
        self.assertIn("Highest sampled process RSS 3.00 GiB", text)
        self.assertIn("lower bound on the true peak and host memory only", text)
        self.assertIn("unknown, not zero", text)
        self.assertIn("Apple-silicon unified memory", text)

    def test_an_unresolvable_process_is_recorded_unavailable_and_the_run_still_goes(
        self,
    ):
        self.c["memory_probe"] = {"process": "no-such-server-8f3a1c"}
        d = evaluate(self.c, self.root / "nf.json", backend=SyntheticBackend())
        self.assertEqual(d["memory_probe"]["reason"], "process_not_found")
        self.assertTrue(all(a["outcome"] == "pass" for a in d["attempts"]))
        self.assertIn("probe unavailable (process_not_found)", report([d]))

    def test_synthetic_and_readiness_only_runs_never_sample(self):
        reader = ScriptedReader([1] * 100)
        d = evaluate(
            self.c,
            self.root / "s.json",
            backend=SyntheticBackend(),
            synthetic=True,
            memory_reader=reader,
        )
        self.assertNotIn("memory_probe", d)
        d = evaluate(
            self.c,
            self.root / "ro.json",
            backend=SyntheticBackend(),
            readiness_only=True,
            memory_reader=reader,
        )
        self.assertNotIn("memory_probe", d)
        self.assertEqual(reader.calls, 0)

    def test_unmeasured_is_reported_as_unknown(self):
        del self.c["memory_probe"]
        d = evaluate(self.c, self.root / "n.json", backend=SyntheticBackend())
        self.assertIn("Memory: not measured (no memory_probe configured)", report([d]))

    def test_schema_rejects_a_tampered_block(self):
        d = self.run_eval([5] * 60)
        validate(d)
        for mutate in (
            lambda x: x["memory_probe"].update(peak_process_rss_bytes=-1),
            lambda x: x["memory_probe"].update(status="guessed"),
            lambda x: x["memory_probe"].update(pid=4242),
            lambda x: x["memory_probe"].pop("gpu_pools"),
            lambda x: x["attempts"][0]["metrics"].update(peak_process_rss_bytes=-5),
        ):
            broken = copy.deepcopy(d)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate(broken)

    def test_the_document_never_carries_the_pid_or_the_endpoint(self):
        d = self.run_eval([5] * 60)
        encoded = str(d)
        self.assertNotIn("4242", encoded)
        self.assertNotIn("127.0.0.1", encoded)

    def test_a_real_process_is_sampled_end_to_end(self):
        if platform.system() not in ("Linux", "Darwin"):
            self.skipTest("Linux and macOS only")
        self.c["memory_probe"] = {"pid": os.getpid()}
        d = evaluate(self.c, self.root / "real.json", backend=SyntheticBackend())
        block = d["memory_probe"]
        self.assertEqual(block["status"], "sampled")
        self.assertGreater(block["peak_process_rss_bytes"], 1_000_000)
        self.assertGreaterEqual(block["samples"], len(d["attempts"]))


class TargetFlowTests(unittest.TestCase):
    """Through `target-run` the run, and so the sampling, happens on the target
    next to the server; the named PID is the target's."""

    @classmethod
    def setUpClass(cls):
        require_isolated_install()

    def test_local_target_samples_the_named_process_and_probes_the_cache(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name).resolve()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        WireHandler.mode = "normal"
        server = ThreadingHTTPServer(("127.0.0.1", 0), WireHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        c = config(ROOT / "src/poundlock_toolkit/data/examples/openai.json")
        c["limits"].update(
            total_seconds=20,
            request_seconds=4,
            repetitions=1,
            prefix_cache_probe_enabled=True,
        )
        c["memory_probe"] = {"pid": child.pid}
        c["endpoint"] = f"http://127.0.0.1:{server.server_port}"
        target = {"transport": "local", "host": None, "python": sys.executable}
        try:
            with patch.dict(os.environ, {"HOME": str(root)}):
                reviewed = target_plan(c, target)
                self.assertEqual(reviewed["evaluation"]["memory_probe"]["requests"], 0)
                self.assertIn("memory_probe", reviewed["evaluation"]["reviewed_inputs"])
                result = start(c, target, reviewed, root / "receipt.json")
            self.assertEqual(result["state"]["status"], "completed")
            doc = result["result"]
            block = doc["memory_probe"]
            self.assertEqual(block["status"], "sampled")
            self.assertGreater(block["peak_process_rss_bytes"], 0)
            self.assertEqual(result["result_sha256"], digest(doc))
            # The wire stub reports no cache counts or timings: the probe ran,
            # and honestly settled nothing.
            self.assertEqual(doc["prefix_cache_probe"]["observed"], "inconclusive")
            self.assertEqual(
                [a["stage"] for a in doc["attempts"]].count("cache_probe"),
                PROBE_REQUESTS,
            )
            saved = read_json(root / "receipt.json")
            self.assertEqual(saved["plan_sha256"], digest(reviewed))
        finally:
            child.kill()
            child.wait()
            server.shutdown()
            server.server_close()
            thread.join()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

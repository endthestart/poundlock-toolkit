"""Measuring the prefix cache: the verdict, the evidence behind it, and how a
measured result sits beside the declared setting."""

import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from poundlock_toolkit.backends import PROBE_MAX_TOKENS, Backend
from poundlock_toolkit.cache_probe import (
    FOLLOW_UP,
    MAX_LINES,
    MIN_LINES,
    PROBE_REQUESTS,
    cached_fraction,
    derive,
    describe,
    finding,
    judge,
    probe_lines,
    probe_prompts,
    probe_request,
)
from poundlock_toolkit.core import Failure, config, read_json, validate
from poundlock_toolkit.demo import SyntheticBackend
from poundlock_toolkit.drift import evaluation_check
from poundlock_toolkit.evaluation import evaluate
from poundlock_toolkit.planning import plan
from poundlock_toolkit.reporting import export, recommendation, report
from poundlock_toolkit.workloads import EXPECTED

ROOT = Path(__file__).resolve().parents[1]
# A result exactly as 0.2.1 wrote it, from a run against a synthetic server.
STORED_021 = ROOT / "tests/data/result-0.2.1-prefix-cache-probe.json"


def known_identity(c, prefix_cache="unknown"):
    """Every identity field but the cache, which is the thing under test."""
    c["model"].update(revision="fixed-revision", quantization="Q4_K_M")
    c["backend"].update(version="10830", application=None)
    c["settings"].update(
        template_sha256="a" * 64, prefix_cache=prefix_cache, kv_quantization="f16"
    )
    c["limits"]["prefix_cache_probe_enabled"] = True
    return c


def row(outcome="pass", **metrics):
    return {"id": str(uuid.uuid4()), "outcome": outcome, "metrics": metrics}


class Server(BaseHTTPRequestHandler):
    """A server with (or without) a prefix cache, reporting it in one of several
    dialects. Tokens are words, and a request's tokens are its messages' words in
    order. The cache is the longest shared word prefix with the last prompt and the
    reply it got, as a single-slot llama.cpp server holds it.

    What a request that diverges inside that slot gets (an edit to the tail of the
    prompt) and what one that only appends after it gets are set separately:
    `on_edit` is `reuse` (a plain prefix cache), `slow` (a hybrid model: part of the
    prompt comes back from an earlier checkpoint but the restore costs more than
    starting cold) or `cold` (nothing comes back)."""

    style = "llamacpp"  # openai | llamacpp | timing_only | silent | stub_zero
    cache = True
    on_edit = "reuse"  # reuse | slow | cold
    on_append = "reuse"  # reuse | cold
    replies = ["OK"]  # what probe requests are answered with, in turn
    slot = ""
    prompt_words = 0  # how many of the slot's words were the prompt
    cold_seconds = 2.0  # reported prompt time for a prompt with nothing cached
    ttft_sleep = 0.0  # real delay before the first token, for the `silent` style
    fail_probe_after = None  # fail the nth probe request onward
    probes = 0
    seen = None  # the messages of the latest request
    probe_messages = []  # the messages of every probe request

    def log_message(self, *_):
        pass

    def reply(self, body, kind="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(
            json.dumps(
                {
                    "data": [{"id": "example-model"}],
                    "models": [{"name": "example-model"}],
                }
            ).encode()
        )

    def held(self, prompt):
        """(prompt tokens served from the slot, the prompt time reported for it)."""
        words, kept = prompt.split(), Server.slot.split()
        total, shared = len(words), 0
        while shared < min(total, len(kept)) and words[shared] == kept[shared]:
            shared += 1
        cold = Server.cold_seconds
        if not Server.cache or shared == 0:
            return 0, cold
        # The last prompt token is always evaluated, so an exact repeat is n - 1.
        cached = min(shared, total - 1)
        appended = shared == len(kept) and total > len(kept)
        repeated = shared == total == Server.prompt_words
        if appended and Server.on_append == "cold":
            return 0, cold
        if not (appended or repeated):
            # Diverges inside the slot: the server must roll back.
            if Server.on_edit == "cold":
                return 0, cold
            if Server.on_edit == "slow":
                return int(shared * 0.58), cold * 2.6
        return cached, cold * (total - cached) / total

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Server.seen = payload["messages"]
        prompt = payload["messages"][0]["content"]
        conversation = " ".join(m["content"] for m in payload["messages"])
        probe = payload.get("max_tokens") == PROBE_MAX_TOKENS or (
            payload.get("options", {}).get("num_predict") == PROBE_MAX_TOKENS
        )
        if probe:
            Server.probes += 1
            Server.probe_messages.append(payload["messages"])
            if (
                Server.fail_probe_after is not None
                and Server.probes >= Server.fail_probe_after
            ):
                return self.reply(b"{}", status=503)
        total = len(conversation.split())
        cached, seconds = self.held(conversation)
        if probe:
            text = Server.replies[(Server.probes - 1) % len(Server.replies)]
        else:
            text = "OK" if prompt == "Reply exactly OK" else json.dumps(EXPECTED)
        Server.slot, Server.prompt_words = f"{conversation} {text}", total
        finish = "length" if probe else "stop"
        if self.path == "/api/chat":
            return self.reply(
                json.dumps(
                    {
                        "message": {"content": text},
                        "done": True,
                        "done_reason": finish,
                        "eval_count": 2,
                        "eval_duration": 10**8,
                        "prompt_eval_count": total - cached,
                        "prompt_eval_duration": int(seconds * 1e9),
                    }
                ).encode()
            )
        usage = {"prompt_tokens": total, "completion_tokens": 2}
        timings = {"prompt_n": total - cached, "prompt_ms": seconds * 1000}
        if Server.style == "openai":
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
            timings = None
        elif Server.style == "llamacpp":
            timings["cache_n"] = cached
        elif Server.style == "stub_zero":
            usage["prompt_tokens_details"] = {"cached_tokens": 0}
        elif Server.style == "silent":
            timings = None
        if payload.get("stream"):
            if Server.style == "silent" and Server.ttft_sleep:
                time.sleep(Server.ttft_sleep * (total - cached) / total + 0.02)
            events = [
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": text}}]},
                {"choices": [{"delta": {}, "finish_reason": finish}]},
                {
                    "choices": [],
                    "usage": usage,
                    **({"timings": timings} if timings else {}),
                },
            ]
            body = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in events)
                + "data: [DONE]\n\n"
            )
            return self.reply(body.encode(), "text/event-stream")
        value = {
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": usage,
        }
        if timings:
            value["timings"] = timings
        self.reply(json.dumps(value).encode())


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Server.style, Server.cache, Server.slot = "llamacpp", True, ""
        Server.on_edit, Server.on_append, Server.replies = "reuse", "reuse", ["OK"]
        Server.cold_seconds, Server.ttft_sleep = 2.0, 0.0
        Server.fail_probe_after, Server.probes, Server.seen = None, 0, None
        Server.probe_messages = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = known_identity(config(ROOT / "examples/openai.json"))
        self.c["endpoint"] = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.tmp.cleanup()

    def run_eval(self, c=None, name="result.json"):
        return evaluate(c or self.c, self.root / name)

    def probe_rows(self, doc):
        return [a for a in doc["attempts"] if a["stage"] == "cache_probe"]


class ProbePromptTests(unittest.TestCase):
    def test_the_edit_only_adds_a_short_suffix_to_the_same_prompt(self):
        base, edited = probe_prompts(8192, "abc123")
        self.assertTrue(edited.startswith(base))
        self.assertGreater(len(edited), len(base))
        self.assertLess(len(edited) - len(base), 40)

    def test_an_agent_turn_appends_new_messages_after_the_reply_verbatim(self):
        base, edited = probe_prompts(8192, "abc123")
        reply = "  OK, noted.\n"
        prompt, history = probe_request("agent_turn", base, edited, reply)
        self.assertEqual(
            list(history),
            [
                {"role": "user", "content": base},
                {"role": "assistant", "content": reply},
            ],
        )
        self.assertEqual(prompt, FOLLOW_UP)
        self.assertLess(len(FOLLOW_UP), 60)

    def test_the_other_requests_are_single_user_messages(self):
        base, edited = probe_prompts(8192, "abc123")
        self.assertEqual(probe_request("base", base, edited, ""), (base, ()))
        self.assertEqual(probe_request("repeat", base, edited, "OK"), (base, ()))
        self.assertEqual(probe_request("edit_inside", base, edited, "OK"), (edited, ()))

    def test_a_run_specific_nonce_leads_the_prompt(self):
        """An earlier run's cache on the same server must not pre-warm this one."""
        first, _ = probe_prompts(8192, "aaaaaaaaaaaa")
        second, _ = probe_prompts(8192, "bbbbbbbbbbbb")
        self.assertNotEqual(first[:40], second[:40])

    def test_length_follows_the_window_within_bounds(self):
        self.assertEqual(probe_lines(512), MIN_LINES)
        self.assertEqual(probe_lines(1_048_576), MAX_LINES)
        for context in (512, 4096, 8192, 131072):
            base, edited = probe_prompts(context, "x")
            # Roughly 30 tokens a line, and the whole request must fit the window.
            self.assertLess(probe_lines(context) * 30 + 64, context)
            self.assertLess(len(edited), context * 4)


def verdict(first, second, third, fourth=None, warmed=True):
    """The verdict from four requests' metrics. The edit inside the last message
    (the fourth) defaults to the agent turn's numbers, so a test about the verdict
    need not say what it did."""
    fourth = third if fourth is None else fourth
    return derive([row(**m) for m in (first, second, third, fourth)], warmed)


class JudgeTests(unittest.TestCase):
    """The verdict from crafted evidence, including the controls that must NOT
    produce a yes or a no."""

    probe = staticmethod(verdict)

    def test_server_reported_cached_tokens_settle_yes(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 4},
            {"input_tokens": 1000, "cached_tokens": 999},
            {"input_tokens": 1010, "cached_tokens": 990},
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("yes", "reuse_on_repeat_and_agent_turn")
        )
        self.assertEqual(v["repeat"]["basis"], "cached_tokens")
        self.assertAlmostEqual(v["repeat"]["cached_fraction"], 0.999)
        self.assertAlmostEqual(v["agent_turn"]["cached_fraction"], 990 / 1010)
        self.assertEqual(v["edit_inside_verdict"], "reused")

    def test_server_reported_zero_cached_tokens_settle_no(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1010, "cached_tokens": 0},
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("no", "no_reuse_on_repeat_and_agent_turn")
        )
        self.assertEqual(v["edit_inside_verdict"], "not_reused")

    def test_a_few_stray_header_tokens_are_not_reuse(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 5},
            {"input_tokens": 1000, "cached_tokens": 8},
            {"input_tokens": 1010, "cached_tokens": 8},
        )
        self.assertEqual(v["observed"], "no")

    def test_llamacpp_counts_without_a_prompt_size_use_prompt_n(self):
        self.assertAlmostEqual(
            cached_fraction(
                {"cached_tokens": 90, "input_tokens": None, "prompt_n": 10}
            ),
            0.9,
        )

    def test_contradictory_counts_are_unmeasurable_not_a_verdict(self):
        self.assertIsNone(
            cached_fraction({"cached_tokens": 2000, "input_tokens": 1000})
        )
        self.assertIsNone(
            cached_fraction({"cached_tokens": None, "input_tokens": 1000})
        )
        self.assertIsNone(cached_fraction({"cached_tokens": 0, "input_tokens": 0}))

    def test_a_partial_match_is_not_judged(self):
        comparison = judge(
            {"input_tokens": 1000}, {"input_tokens": 1000, "cached_tokens": 300}, True
        )
        self.assertIsNone(comparison["reused"])

    def test_timing_alone_settles_yes_after_a_warmup(self):
        v = self.probe(
            {"prompt_processing_seconds": 2.0},
            {"prompt_processing_seconds": 0.05},
            {"prompt_processing_seconds": 0.12},
        )
        self.assertEqual(v["observed"], "yes")
        self.assertEqual(v["repeat"]["basis"], "prompt_processing_time")
        self.assertAlmostEqual(v["repeat"]["speedup"], 40.0)
        self.assertIsNone(v["repeat"]["cached_fraction"])

    def test_timing_alone_settles_no_when_a_measurable_first_request_is_not_beaten(
        self,
    ):
        v = self.probe(
            {"first_event_seconds": 2.0},
            {"first_event_seconds": 1.9},
            {"first_event_seconds": 2.1},
        )
        self.assertEqual(
            (v["observed"], v["repeat"]["basis"]), ("no", "first_token_time")
        )

    def test_timing_is_not_trusted_without_a_passed_warmup(self):
        """Negative control: the first request may carry one-off costs, and a
        faster repeat would then look like a hit."""
        v = self.probe(
            {"first_event_seconds": 2.0},
            {"first_event_seconds": 0.05},
            {"first_event_seconds": 0.06},
            warmed=False,
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("inconclusive", "insufficient_measurements")
        )
        self.assertFalse(v["warmup_passed"])
        self.assertEqual(v["edit_inside_verdict"], "inconclusive")

    def test_a_fast_first_request_says_nothing_about_the_cache(self):
        v = self.probe(
            {"first_event_seconds": 0.05},
            {"first_event_seconds": 0.05},
            {"first_event_seconds": 0.05},
        )
        self.assertEqual(v["observed"], "inconclusive")

    def test_the_two_clocks_are_never_compared(self):
        v = self.probe(
            {"prompt_processing_seconds": 2.0, "first_event_seconds": None},
            {"prompt_processing_seconds": None, "first_event_seconds": 0.05},
            {"prompt_processing_seconds": None, "first_event_seconds": 0.05},
        )
        self.assertEqual(v["observed"], "inconclusive")

    def test_a_counter_stuck_at_zero_that_timing_contradicts_is_conflicting(self):
        """Negative control for a server that always reports zero cached tokens."""
        v = self.probe(
            {
                "input_tokens": 1000,
                "cached_tokens": 0,
                "prompt_processing_seconds": 2.0,
            },
            {
                "input_tokens": 1000,
                "cached_tokens": 0,
                "prompt_processing_seconds": 0.05,
            },
            {
                "input_tokens": 1010,
                "cached_tokens": 0,
                "prompt_processing_seconds": 0.1,
            },
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("inconclusive", "conflicting_evidence")
        )
        self.assertEqual(v["repeat"]["basis"], "conflicting")
        self.assertEqual(v["agent_turn"]["basis"], "conflicting")

    def test_a_conflict_on_the_agent_turn_alone_is_conflicting_too(self):
        v = self.probe(
            {
                "input_tokens": 1000,
                "cached_tokens": 0,
                "prompt_processing_seconds": 2.0,
            },
            {
                "input_tokens": 1000,
                "cached_tokens": 999,
                "prompt_processing_seconds": 0.05,
            },
            {
                "input_tokens": 1010,
                "cached_tokens": 990,
                "prompt_processing_seconds": 2.5,
            },
        )
        self.assertEqual(v["reason"], "conflicting_evidence")
        self.assertEqual(v["agent_turn"]["basis"], "conflicting")

    def test_counts_win_over_timing_that_merely_looks_ambiguous(self):
        v = self.probe(
            {
                "input_tokens": 1000,
                "cached_tokens": 0,
                "prompt_processing_seconds": 2.0,
            },
            {
                "input_tokens": 1000,
                "cached_tokens": 0,
                "prompt_processing_seconds": 1.7,
            },
            {
                "input_tokens": 1010,
                "cached_tokens": 0,
                "prompt_processing_seconds": 1.9,
            },
        )
        self.assertEqual(v["observed"], "no")

    def test_reuse_on_the_repeat_only_is_inconclusive(self):
        """Negative control: a cache that only serves exact repeats is not a cache
        an agent loop can use, and not an absent one either."""
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 999},
            {"input_tokens": 1010, "cached_tokens": 0},
            {"input_tokens": 1010, "cached_tokens": 1000},
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("inconclusive", "reuse_on_one_request_only")
        )

    def test_reuse_on_the_agent_turn_only_is_inconclusive(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1010, "cached_tokens": 1000},
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("inconclusive", "reuse_on_one_request_only")
        )

    def test_one_missing_measurement_is_not_a_no(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 0},
            {},
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("inconclusive", "insufficient_measurements")
        )

    def test_a_missing_measurement_for_the_edit_alone_leaves_the_verdict(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 999},
            {"input_tokens": 1010, "cached_tokens": 1000},
            {},
        )
        self.assertEqual(v["observed"], "yes")
        self.assertEqual(v["edit_inside_verdict"], "inconclusive")

    def rows(self, outcomes):
        """Four rows that would each settle as reuse, with the given outcomes."""
        first = row(outcomes[0], input_tokens=1000, cached_tokens=0)
        return [
            first,
            *(row(o, input_tokens=1000, cached_tokens=990) for o in outcomes[1:]),
        ]

    def test_a_failed_or_skipped_request_before_the_edit_makes_it_incomplete(self):
        for outcome in ("timeout", "skipped", "infrastructure_failure"):
            for at in range(3):
                with self.subTest(outcome=outcome, at=at):
                    outcomes = ["pass"] * 4
                    outcomes[at] = outcome
                    v = derive(self.rows(outcomes), True)
                    self.assertEqual(
                        (v["observed"], v["reason"]),
                        ("inconclusive", "probe_incomplete"),
                    )
        self.assertEqual(derive([], True)["reason"], "probe_incomplete")
        # A probe cut short of its requests is incomplete however the rest went.
        self.assertEqual(
            derive(self.rows(["pass"] * 3), True)["reason"], "probe_incomplete"
        )

    def test_a_failed_edit_request_leaves_the_verdict_and_has_no_comparison(self):
        """A slow rollback that times out, or is refused, must not throw away a
        repeat and an agent turn that were measured."""
        for outcome in ("timeout", "skipped", "infrastructure_failure"):
            with self.subTest(outcome=outcome):
                v = derive(self.rows(["pass", "pass", "pass", outcome]), True)
                self.assertEqual(
                    (v["observed"], v["reason"]),
                    ("yes", "reuse_on_repeat_and_agent_turn"),
                )
                self.assertEqual(v["edit_inside_verdict"], "inconclusive")
                self.assertIsNone(v["edit_inside"]["reused"])
                self.assertIsNone(v["edit_inside"]["cached_fraction"])
                self.assertEqual(len(v["attempt_ids"]), 4)


class EditInsideTests(unittest.TestCase):
    """The edit inside the last message beside a verdict it must not change. The
    numbers are the first live run, on a hybrid model."""

    probe = staticmethod(verdict)

    lab = (
        {
            "input_tokens": 1240,
            "cached_tokens": 0,
            "prompt_processing_seconds": 1.42,
        },
        {
            "input_tokens": 1240,
            "cached_tokens": 1234,
            "prompt_processing_seconds": 0.08,
        },
        {
            "input_tokens": 1262,
            "cached_tokens": 1236,
            "prompt_processing_seconds": 0.11,
        },
    )

    def test_a_count_that_says_reused_beside_a_slower_request_is_inconclusive(self):
        v = self.probe(
            *self.lab,
            {
                "input_tokens": 1245,
                "cached_tokens": 722,
                "prompt_processing_seconds": 3.71,
            },
        )
        self.assertEqual(
            (v["observed"], v["reason"]), ("yes", "reuse_on_repeat_and_agent_turn")
        )
        self.assertEqual(v["edit_inside"]["basis"], "conflicting")
        self.assertAlmostEqual(v["edit_inside"]["cached_fraction"], 722 / 1245)
        self.assertLess(v["edit_inside"]["speedup"], 1)
        self.assertEqual(v["edit_inside_verdict"], "inconclusive")

    def test_no_reuse_on_the_edit_is_reported_and_leaves_a_yes(self):
        v = self.probe(*self.lab, {"input_tokens": 1245, "cached_tokens": 0})
        self.assertEqual(v["observed"], "yes")
        self.assertEqual(v["edit_inside_verdict"], "not_reused")

    def test_reuse_on_the_edit_cannot_rescue_a_cache_that_the_rest_says_is_off(self):
        v = self.probe(
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1000, "cached_tokens": 0},
            {"input_tokens": 1010, "cached_tokens": 0},
            {"input_tokens": 1010, "cached_tokens": 1000},
        )
        self.assertEqual(v["observed"], "no")
        self.assertEqual(v["edit_inside_verdict"], "reused")

    def test_the_edit_is_judged_against_the_first_request_like_the_others(self):
        v = self.probe(
            {"prompt_processing_seconds": 2.0},
            {"prompt_processing_seconds": 0.05},
            {"prompt_processing_seconds": 0.12},
            {"prompt_processing_seconds": 2.2},
        )
        self.assertEqual(v["observed"], "yes")
        self.assertEqual(v["edit_inside"]["basis"], "prompt_processing_time")
        self.assertEqual(v["edit_inside_verdict"], "not_reused")

    def test_a_slow_request_reads_as_slower_not_as_a_fraction(self):
        text = describe(
            {
                "reused": None,
                "basis": "conflicting",
                "cached_fraction": 0.58,
                "speedup": 0.38,
            }
        )
        self.assertIn("58% of the prompt tokens cached", text)
        self.assertIn("this request took 2.6x as long as the first", text)
        self.assertIn(
            "the first request took 17.5x as long",
            describe(
                {
                    "reused": True,
                    "basis": "prompt_processing_time",
                    "cached_fraction": None,
                    "speedup": 17.5,
                }
            ),
        )


class DeclaredAgainstMeasuredTests(unittest.TestCase):
    def doc(self, declared, observed):
        doc = {"settings": {"prefix_cache": declared}}
        if observed is not None:
            doc["prefix_cache_probe"] = {"observed": observed}
        return doc

    def test_the_measured_value_wins_and_a_disagreement_is_flagged(self):
        cases = [
            # declared, observed, effective, mismatch
            ("unknown", "yes", "enabled", False),
            ("unknown", "no", "disabled", False),
            ("enabled", "yes", "enabled", False),
            ("disabled", "no", "disabled", False),
            ("enabled", "no", "disabled", True),
            ("disabled", "yes", "enabled", True),
            # Not settled, or not measured: the declaration stands, no mismatch.
            ("enabled", "inconclusive", "enabled", False),
            ("disabled", None, "disabled", False),
            ("unknown", "inconclusive", "unknown", False),
        ]
        for declared, observed, effective, mismatch in cases:
            with self.subTest(declared=declared, observed=observed):
                f = finding(self.doc(declared, observed))
                self.assertEqual((f["effective"], f["mismatch"]), (effective, mismatch))
                self.assertEqual(f["declared"], declared)

    def test_only_the_repeat_and_the_agent_turn_decide_what_is_checked(self):
        """A server that reuses on repeat and on append has a prefix cache, however
        an edit to the tail of the prompt goes for it."""
        for edit, expected in (
            ({"input_tokens": 1000, "cached_tokens": 0}, "not_reused"),
            (
                {
                    "input_tokens": 1000,
                    "cached_tokens": 600,
                    "prompt_processing_seconds": 3.0,
                },
                "inconclusive",
            ),
        ):
            probe = verdict(
                {
                    "input_tokens": 1000,
                    "cached_tokens": 0,
                    "prompt_processing_seconds": 1.0,
                },
                {
                    "input_tokens": 1000,
                    "cached_tokens": 995,
                    "prompt_processing_seconds": 0.1,
                },
                {
                    "input_tokens": 1010,
                    "cached_tokens": 995,
                    "prompt_processing_seconds": 0.1,
                },
                edit,
            )
            self.assertEqual(probe["edit_inside_verdict"], expected)
            for declared, mismatch in (("enabled", False), ("disabled", True)):
                with self.subTest(edit=expected, declared=declared):
                    doc = {
                        "settings": {"prefix_cache": declared},
                        "prefix_cache_probe": probe,
                    }
                    f = finding(doc)
                    self.assertEqual(
                        (f["effective"], f["mismatch"]), ("enabled", mismatch)
                    )


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "examples/openai.json")

    def test_off_by_default_and_the_plan_is_unchanged(self):
        p = plan(self.c)
        self.assertNotIn("prefix_cache_probe", p)
        self.assertNotIn("prefix-cache probe (unscored)", p["stages"])
        self.assertEqual(p["maximum_direct_requests"], 7)

    def test_enabled_probe_is_in_the_plan_and_its_budget(self):
        self.c["limits"]["prefix_cache_probe_enabled"] = True
        p = plan(self.c)
        self.assertEqual(p["maximum_direct_requests"], 7 + PROBE_REQUESTS)
        self.assertEqual(p["maximum_direct_requests"], 11)
        stages = p["stages"]
        self.assertEqual(
            stages[stages.index("warmup (unscored)") + 1],
            "prefix-cache probe (unscored)",
        )
        self.assertEqual(p["prefix_cache_probe"]["requests"], PROBE_REQUESTS)
        self.assertEqual(PROBE_REQUESTS, 4)
        self.assertFalse(p["prefix_cache_probe"]["scored"])
        sequence = p["prefix_cache_probe"]["sequence"]
        self.assertEqual(len(sequence), PROBE_REQUESTS)
        self.assertIn("agent turn", sequence[2])
        self.assertIn("edit inside the last message", sequence[3])
        self.assertIn("edit inside the last message", p["prefix_cache_probe"]["note"])

    def test_readiness_only_never_probes(self):
        self.c["limits"]["prefix_cache_probe_enabled"] = True
        p = plan(self.c, readiness_only=True)
        self.assertEqual(p["maximum_direct_requests"], 1)
        self.assertNotIn("prefix_cache_probe", p)

    def test_turning_the_probe_on_after_review_is_drift(self):
        reviewed = plan(self.c)
        self.c["limits"]["prefix_cache_probe_enabled"] = True
        check = evaluation_check(reviewed, plan(self.c))
        self.assertEqual(check["status"], "stale")
        self.assertEqual(check["drifted"], ["limits"])

    def test_a_plan_reviewed_with_the_shorter_probe_is_stale(self):
        """An approval covers the requests it named; the probe now sends one more."""
        self.c["limits"]["prefix_cache_probe_enabled"] = True
        current = plan(self.c)
        reviewed = copy.deepcopy(current)
        reviewed["maximum_direct_requests"] -= 1
        reviewed["prefix_cache_probe"]["requests"] -= 1
        del reviewed["prefix_cache_probe"]["sequence"]
        check = evaluation_check(reviewed, current)
        self.assertEqual((check["check"], check["status"]), ("plan_version", "stale"))

    def test_the_probe_cannot_be_combined_with_a_tool_schema_scenario(self):
        c = config(ROOT / "examples/tool-schema.json")
        c["limits"]["prefix_cache_probe_enabled"] = True
        with self.assertRaises(ValueError):
            validate(c, "config.schema.json")


class BackendTests(ServerCase):
    def test_probe_reads_openai_style_cached_tokens(self):
        Server.style = "openai"
        backend = Backend(self.c)
        first = backend.probe("alpha beta gamma delta")["metrics"]
        again = backend.probe("alpha beta gamma delta")["metrics"]
        self.assertEqual(first["cached_tokens"], 0)
        self.assertEqual(again["cached_tokens"], 3)
        self.assertEqual(again["input_tokens"], 4)
        self.assertIsNone(again["prompt_processing_seconds"])

    def test_probe_reads_llamacpp_cache_n_and_prompt_time(self):
        Server.style = "llamacpp"
        backend = Backend(self.c)
        backend.probe("alpha beta gamma delta")
        again = backend.probe("alpha beta gamma delta")["metrics"]
        self.assertEqual(again["cached_tokens"], 3)
        self.assertAlmostEqual(again["prompt_processing_seconds"], 0.5)
        self.assertEqual(again["prompt_n"], 1)
        self.assertIsNotNone(again["first_event_seconds"])

    def test_a_server_that_reports_nothing_leaves_the_count_null(self):
        Server.style = "silent"
        m = Backend(self.c).probe("alpha beta gamma delta")["metrics"]
        self.assertIsNone(m["cached_tokens"])
        self.assertIsNone(m["prompt_processing_seconds"])

    def test_the_probe_asks_for_a_short_reply_and_accepts_a_cut_off_one(self):
        # The stub answers `length` to a probe; a scored request would reject that.
        Backend(self.c).probe("alpha beta")
        with self.assertRaises(Failure) as failure:
            payload = {
                "model": "example-model",
                "messages": [{"role": "user", "content": "x y"}],
                "max_tokens": PROBE_MAX_TOKENS,
                "stream": True,
            }
            Backend(self.c).streaming(payload)
        self.assertEqual(failure.exception.outcome, "completion_failure")

    def test_a_probe_can_follow_earlier_turns(self):
        history = [
            {"role": "user", "content": "alpha beta"},
            {"role": "assistant", "content": "OK"},
        ]
        Backend(self.c).probe("gamma delta", history)
        self.assertEqual(
            Server.seen, [*history, {"role": "user", "content": "gamma delta"}]
        )
        Backend(self.c).probe("gamma delta")
        self.assertEqual(Server.seen, [{"role": "user", "content": "gamma delta"}])

    def test_native_ollama_sends_earlier_turns_too(self):
        self.c["api"] = "ollama"
        self.c["backend"]["name"] = "ollama"
        history = [
            {"role": "user", "content": "alpha beta"},
            {"role": "assistant", "content": "OK"},
        ]
        Backend(self.c).probe("gamma delta", history)
        self.assertEqual(
            [m["role"] for m in Server.seen], ["user", "assistant", "user"]
        )

    def test_native_ollama_reports_prompt_time_but_no_cached_count(self):
        self.c["api"] = "ollama"
        self.c["backend"]["name"] = "ollama"
        backend = Backend(self.c)
        backend.probe("alpha beta gamma delta")
        m = backend.probe("alpha beta gamma delta")["metrics"]
        self.assertIsNone(m["cached_tokens"])
        self.assertAlmostEqual(m["prompt_processing_seconds"], 0.5)
        self.assertIsNone(m["first_event_seconds"])


class EvaluationTests(ServerCase):
    def test_a_working_cache_is_observed_and_completes_an_unknown_identity(self):
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "yes")
        self.assertEqual(probe["repeat"]["basis"], "cached_tokens")
        self.assertGreater(probe["repeat"]["cached_fraction"], 0.9)
        self.assertGreater(probe["agent_turn"]["cached_fraction"], 0.9)
        self.assertGreater(probe["edit_inside"]["cached_fraction"], 0.9)
        self.assertEqual(probe["reason"], "reuse_on_repeat_and_agent_turn")
        self.assertEqual(probe["edit_inside_verdict"], "reused")
        self.assertNotIn("extension", probe)
        # Declared setting untouched; identity now complete through the measurement.
        self.assertEqual(d["settings"]["prefix_cache"], "unknown")
        self.assertTrue(d["identity_complete"])
        r = recommendation(d, "extraction")
        self.assertEqual(r["level"], "repeated task qualification")
        self.assertEqual(r["checks"]["identity"], "pass")
        validate(d)

    def test_a_cache_that_is_off_is_observed_too(self):
        Server.cache = False
        d = self.run_eval()
        self.assertEqual(d["prefix_cache_probe"]["observed"], "no")
        self.assertEqual(d["prefix_cache_probe"]["edit_inside_verdict"], "not_reused")
        self.assertTrue(d["identity_complete"])

    def test_a_hybrid_model_reuses_on_append_but_pays_for_an_edit_inside(self):
        """The first live run: the repeat and the agent turn reuse the cache, and an
        edit inside the last message comes back part cached and slower than cold."""
        Server.on_edit = "slow"
        c = known_identity(copy.deepcopy(self.c), "enabled")
        d = self.run_eval(c)
        probe = d["prefix_cache_probe"]
        self.assertEqual(
            (probe["observed"], probe["reason"]),
            ("yes", "reuse_on_repeat_and_agent_turn"),
        )
        self.assertGreater(probe["agent_turn"]["cached_fraction"], 0.9)
        self.assertGreater(probe["agent_turn"]["speedup"], 2)
        edit = probe["edit_inside"]
        self.assertEqual(edit["basis"], "conflicting")
        self.assertAlmostEqual(edit["cached_fraction"], 0.58, delta=0.02)
        self.assertLess(edit["speedup"], 1)
        self.assertEqual(probe["edit_inside_verdict"], "inconclusive")
        validate(d)
        # The declaration is checked against the verdict, not against the edit.
        self.assertFalse(finding(d)["mismatch"])
        self.assertEqual(recommendation(d, "extraction")["checks"]["identity"], "pass")
        text = report([d])
        self.assertIn("Prefix cache: observed yes", text)
        self.assertIn("declared enabled, consistent", text)
        self.assertIn("agent turn: 99% of the prompt tokens cached", text)
        self.assertIn("Edit inside the last message: inconclusive", text)
        self.assertIn("this request took 2.6x as long as the first", text)

    def test_an_edit_that_gets_nothing_back_does_not_undo_a_yes(self):
        Server.on_edit = "cold"
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "yes")
        self.assertEqual(probe["edit_inside_verdict"], "not_reused")
        self.assertEqual(probe["edit_inside"]["basis"], "cached_tokens")
        self.assertIn("Edit inside the last message: not reused", report([d]))

    def test_a_cache_that_only_serves_exact_repeats_is_not_a_yes(self):
        Server.on_append = "cold"
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(
            (probe["observed"], probe["reason"]),
            ("inconclusive", "reuse_on_one_request_only"),
        )
        self.assertTrue(probe["repeat"]["reused"])
        self.assertFalse(probe["agent_turn"]["reused"])
        self.assertFalse(d["identity_complete"])
        text = report([d])
        self.assertIn("only one of the repeat and the agent turn", text)

    def test_the_agent_turn_appends_the_reply_the_server_returned_verbatim(self):
        Server.replies = [" Okay,\nnoted "]
        d = self.run_eval()
        base, repeat, agent, edit = Server.probe_messages
        self.assertEqual([m["role"] for m in base], ["user"])
        self.assertEqual(repeat, base)
        self.assertEqual(
            agent,
            [
                base[0],
                {"role": "assistant", "content": " Okay,\nnoted "},
                {"role": "user", "content": FOLLOW_UP},
            ],
        )
        self.assertEqual([m["role"] for m in edit], ["user"])
        self.assertTrue(edit[0]["content"].startswith(base[0]["content"]))
        self.assertGreater(len(edit[0]["content"]), len(base[0]["content"]))
        self.assertEqual(d["prefix_cache_probe"]["observed"], "yes")

    def test_the_agent_turn_follows_the_latest_reply_when_replies_differ(self):
        """The server holds the reply to the request before, so that is what an
        agent's next turn appends after. On a hybrid server, appending after any
        other text would be an edit inside the slot and would be charged as one."""
        Server.on_edit = "slow"
        Server.replies = ["first", "second"]
        d = self.run_eval()
        agent = Server.probe_messages[2]
        self.assertEqual(agent[1], {"role": "assistant", "content": "second"})
        self.assertEqual(d["prefix_cache_probe"]["observed"], "yes")
        self.assertGreater(
            d["prefix_cache_probe"]["agent_turn"]["cached_fraction"], 0.9
        )

    def test_the_reply_is_not_written_to_the_document(self):
        Server.replies = ["a reply that must stay off the record"]
        d = self.run_eval()
        self.assertNotIn("must stay off the record", json.dumps(d))

    def test_probe_attempts_are_ordered_unscored_and_leave_scoring_alone(self):
        d = self.run_eval()
        stages = [a["stage"] for a in d["attempts"]]
        self.assertEqual(
            stages[:6],
            ["readiness", "warmup", *["cache_probe"] * PROBE_REQUESTS],
        )
        probes = self.probe_rows(d)
        self.assertEqual([a["repetition"] for a in probes], [1, 2, 3, 4])
        for a in probes:
            self.assertEqual(a["workload"], "cache_probe")
            self.assertEqual(a["outcome"], "pass")
            self.assertIsNone(a["independent_pass"])
            self.assertIsNone(a["completion_pass"])
        # Same prompt twice, then two different requests.
        hashes = [a["prompt_sha256"] for a in probes]
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(len(set(hashes)), 3)
        # Three extraction repetitions still score exactly as before.
        r = recommendation(d, "extraction")
        self.assertEqual((r["passed"], r["attempted"], r["planned"]), (3, 3, 3))

    def test_requests_sent_match_the_planned_budget(self):
        counted = plan(self.c)["maximum_direct_requests"]
        d = self.run_eval()
        self.assertLessEqual(len(d["attempts"]), counted)
        self.assertEqual(Server.probes, PROBE_REQUESTS)

    def test_the_stored_verdict_can_be_rederived_from_the_attempts(self):
        d = self.run_eval()
        again = derive(self.probe_rows(d), d["prefix_cache_probe"]["warmup_passed"])
        self.assertEqual(again, d["prefix_cache_probe"])
        self.assertEqual(
            d["prefix_cache_probe"]["attempt_ids"],
            [a["id"] for a in self.probe_rows(d)],
        )

    def test_a_wrong_declaration_is_reported_as_a_mismatch_and_blocks_qualification(
        self,
    ):
        for declared, cache, expected in (
            ("enabled", False, "no"),
            ("disabled", True, "yes"),
        ):
            with self.subTest(declared=declared):
                Server.cache, Server.slot = cache, ""
                c = known_identity(copy.deepcopy(self.c), declared)
                d = self.run_eval(c, f"{declared}.json")
                self.assertEqual(d["prefix_cache_probe"]["observed"], expected)
                r = recommendation(d, "extraction")
                self.assertEqual(r["checks"]["identity"], "fail")
                self.assertEqual(r["level"], "insufficient evidence")
                self.assertTrue(
                    any("contradicts the measured" in x for x in r["reasons"])
                )
                self.assertIn("Correct settings.prefix_cache", r["next_test"])
                text = report([d])
                self.assertIn("Prefix cache: observed " + expected, text)
                self.assertIn("MISMATCH", text)

    def test_a_correct_declaration_is_reported_consistent(self):
        c = known_identity(copy.deepcopy(self.c), "enabled")
        d = self.run_eval(c)
        text = report([d])
        self.assertIn("declared enabled, consistent", text)
        self.assertNotIn("MISMATCH", text)
        self.assertEqual(recommendation(d, "extraction")["checks"]["identity"], "pass")

    def test_the_report_says_when_the_cache_was_not_measured(self):
        c = known_identity(copy.deepcopy(self.c), "disabled")
        del c["limits"]["prefix_cache_probe_enabled"]
        d = self.run_eval(c)
        self.assertNotIn("prefix_cache_probe", d)
        self.assertIn("Prefix cache: not measured (declared disabled", report([d]))

    def test_timing_alone_over_the_wire_after_a_warmup(self):
        Server.style = "timing_only"
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "yes")
        self.assertTrue(probe["warmup_passed"])
        self.assertEqual(probe["repeat"]["basis"], "prompt_processing_time")

    def test_first_token_time_alone_over_the_wire(self):
        Server.style, Server.ttft_sleep = "silent", 0.6
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "yes")
        self.assertEqual(probe["repeat"]["basis"], "first_token_time")
        self.assertGreater(probe["repeat"]["speedup"], 2)

    def test_first_token_time_alone_settles_no_when_nothing_is_cached(self):
        Server.style, Server.ttft_sleep, Server.cache = "silent", 0.6, False
        d = self.run_eval()
        self.assertEqual(d["prefix_cache_probe"]["observed"], "no")

    def test_disabling_the_warmup_withdraws_trust_in_timing(self):
        Server.style = "timing_only"
        self.c["limits"]["warmup_enabled"] = False
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "inconclusive")
        self.assertFalse(probe["warmup_passed"])
        # Nothing to complete the identity with: still unknown.
        self.assertFalse(d["identity_complete"])

    def test_a_counter_stuck_at_zero_that_timing_contradicts(self):
        Server.style = "stub_zero"
        d = self.run_eval()
        self.assertEqual(d["prefix_cache_probe"]["reason"], "conflicting_evidence")
        self.assertFalse(d["identity_complete"])

    def test_native_ollama_over_the_wire_uses_prompt_time(self):
        self.c["api"] = "ollama"
        self.c["backend"]["name"] = "ollama"
        d = self.run_eval()
        probe = d["prefix_cache_probe"]
        self.assertEqual(probe["observed"], "yes")
        self.assertEqual(probe["repeat"]["basis"], "prompt_processing_time")
        self.assertIsNone(probe["repeat"]["cached_fraction"])

    def test_a_failing_probe_request_ends_the_probe_and_leaves_identity_unknown(self):
        Server.fail_probe_after = 2
        d = self.run_eval()
        rows = self.probe_rows(d)
        self.assertEqual(
            [a["outcome"] for a in rows],
            ["pass", "infrastructure_failure", "skipped", "skipped"],
        )
        self.assertEqual(d["prefix_cache_probe"]["reason"], "probe_incomplete")
        self.assertFalse(d["identity_complete"])
        # Unscored: the probe's failure does not touch the gates or the task.
        self.assertEqual(recommendation(d, "extraction")["passed"], 3)
        self.assertEqual(Server.probes, 2)

    def test_a_failing_agent_turn_makes_the_probe_incomplete(self):
        Server.fail_probe_after = 3
        d = self.run_eval()
        self.assertEqual(
            [a["outcome"] for a in self.probe_rows(d)],
            ["pass", "pass", "infrastructure_failure", "skipped"],
        )
        self.assertEqual(d["prefix_cache_probe"]["reason"], "probe_incomplete")
        self.assertEqual(d["prefix_cache_probe"]["edit_inside_verdict"], "inconclusive")
        self.assertFalse(d["identity_complete"])

    def test_a_failing_edit_request_leaves_the_verdict_standing(self):
        """A rollback slow enough to time out is exactly what the edit may cost."""
        Server.fail_probe_after = 4
        d = self.run_eval()
        rows = self.probe_rows(d)
        self.assertEqual(
            [a["outcome"] for a in rows],
            ["pass", "pass", "pass", "infrastructure_failure"],
        )
        probe = d["prefix_cache_probe"]
        self.assertEqual(
            (probe["observed"], probe["reason"]),
            ("yes", "reuse_on_repeat_and_agent_turn"),
        )
        self.assertEqual(probe["edit_inside_verdict"], "inconclusive")
        self.assertIsNone(probe["edit_inside"]["reused"])
        self.assertEqual(len(probe["attempt_ids"]), 4)
        self.assertTrue(d["identity_complete"])
        self.assertIn(
            "Edit inside the last message: inconclusive (no usable measurement)",
            report([d]),
        )
        validate(d)

    def test_a_reply_with_no_text_leaves_nothing_to_append_after(self):
        """An agent turn extends a reply; without one it is not sent, and the edit
        after it is not either."""
        Server.replies = [""]
        d = self.run_eval()
        rows = self.probe_rows(d)
        self.assertEqual(
            [a["outcome"] for a in rows], ["pass", "pass", "skipped", "skipped"]
        )
        self.assertEqual([a["code"] for a in rows[2:]], ["not_run", "not_run"])
        self.assertEqual(d["prefix_cache_probe"]["reason"], "probe_incomplete")
        self.assertEqual(Server.probes, 2)
        self.assertFalse(d["identity_complete"])

    def test_a_failed_readiness_skips_the_whole_probe(self):
        class Down(SyntheticBackend):
            def readiness(self):
                raise Failure("infrastructure_failure", "unavailable")

        d = evaluate(self.c, self.root / "down.json", backend=Down())
        self.assertEqual(
            [a["outcome"] for a in self.probe_rows(d)], ["skipped"] * PROBE_REQUESTS
        )
        self.assertEqual(d["prefix_cache_probe"]["reason"], "probe_incomplete")

    def test_the_synthetic_demo_backend_measures_nothing_and_says_so(self):
        d = evaluate(
            self.c, self.root / "demo.json", backend=SyntheticBackend(), synthetic=True
        )
        self.assertEqual(d["prefix_cache_probe"]["reason"], "insufficient_measurements")
        self.assertFalse(d["identity_complete"])

    def test_export_and_schema_reject_a_tampered_verdict(self):
        d = self.run_eval()
        validate(d)
        for mutate in (
            lambda x: x["prefix_cache_probe"].update(observed="maybe"),
            lambda x: x["prefix_cache_probe"].update(invented=True),
            lambda x: x["prefix_cache_probe"]["repeat"].update(cached_fraction=1.5),
            lambda x: x["prefix_cache_probe"].pop("thresholds"),
            lambda x: x["attempts"][2]["metrics"].update(cached_tokens=-1),
            lambda x: x["prefix_cache_probe"].update(edit_inside_verdict="slow"),
            lambda x: x["prefix_cache_probe"].pop("edit_inside_verdict"),
            lambda x: x["prefix_cache_probe"].pop("agent_turn"),
            # One shape or the other, never both.
            lambda x: x["prefix_cache_probe"].update(
                extension=x["prefix_cache_probe"]["repeat"]
            ),
            lambda x: x["prefix_cache_probe"]["attempt_ids"].append(str(uuid.uuid4())),
        ):
            broken = copy.deepcopy(d)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate(broken)

    def test_probe_rows_show_in_the_report_table(self):
        text = report([self.run_eval()])
        self.assertEqual(text.count("| cache_probe | cache_probe |"), PROBE_REQUESTS)
        self.assertIn("Prefix cache: observed yes", text)
        self.assertIn("server-reported", text)
        self.assertIn("agent turn:", text)
        self.assertIn("Edit inside the last message: reused", text)
        self.assertNotIn("extension", text)


class StoredResultTests(unittest.TestCase):
    """A result stored by 0.2.1 has the old probe block (a repeat and an extension,
    three attempts, no agent turn). It must keep validating, exporting and reporting
    as it was, and be read the way it was decided."""

    def setUp(self):
        self.doc = read_json(STORED_021)
        self.now = datetime.fromisoformat(self.doc["created_at"])

    def report(self, doc):
        return report([doc], now=self.now)

    def test_the_stored_result_has_the_old_shape(self):
        probe = self.doc["prefix_cache_probe"]
        self.assertEqual(self.doc["toolkit_version"], "0.2.1")
        self.assertIn("extension", probe)
        for absent in ("agent_turn", "edit_inside", "edit_inside_verdict"):
            self.assertNotIn(absent, probe)
        self.assertEqual(len(probe["attempt_ids"]), 3)
        rows = [a for a in self.doc["attempts"] if a["stage"] == "cache_probe"]
        self.assertEqual(len(rows), 3)

    def test_it_still_validates_and_exports(self):
        validate(self.doc)
        self.assertEqual(export(self.doc), self.doc)

    def test_it_is_read_and_reported_as_a_verdict_on_the_repeat_and_extension(self):
        f = finding(self.doc)
        self.assertEqual((f["observed"], f["measured"]), ("yes", "enabled"))
        text = self.report(self.doc)
        self.assertIn("Prefix cache: observed yes", text)
        self.assertIn("repeat: ", text)
        self.assertIn("; extension: ", text)
        self.assertNotIn("agent turn", text)
        self.assertNotIn("Edit inside the last message", text)

    def test_the_first_live_run_still_reads_as_it_was_recorded(self):
        """conflicting evidence: a cached count beside a request slower than cold."""
        self.doc["prefix_cache_probe"].update(
            observed="inconclusive",
            reason="conflicting_evidence",
            repeat={
                "reused": True,
                "basis": "cached_tokens",
                "cached_fraction": 0.997,
                "speedup": 17.5,
            },
            extension={
                "reused": None,
                "basis": "conflicting",
                "cached_fraction": 0.58,
                "speedup": 0.38,
            },
        )
        validate(self.doc)
        text = self.report(self.doc)
        self.assertIn(
            "Prefix cache: probe inconclusive (the cached-token count and the timing "
            "disagreed)",
            text,
        )
        self.assertEqual(finding(self.doc)["effective"], "unknown")

    def test_old_reasons_keep_their_wording(self):
        for reason, words in (
            ("reuse_on_one_request_only", "only one of the repeat and the extension"),
            ("probe_incomplete", "not all three probe requests passed"),
            ("insufficient_measurements", "too little to settle both comparisons"),
        ):
            with self.subTest(reason=reason):
                doc = copy.deepcopy(self.doc)
                doc["prefix_cache_probe"].update(observed="inconclusive", reason=reason)
                validate(doc)
                self.assertIn(words, self.report(doc))

    def test_a_declaration_the_old_verdict_contradicts_still_blocks_qualification(
        self,
    ):
        self.doc["settings"]["prefix_cache"] = "disabled"
        r = recommendation(self.doc, "extraction", now=self.now)
        self.assertEqual(r["checks"]["identity"], "fail")
        self.assertTrue(
            any(
                "the repeat and the extension both reused the prompt" in x
                for x in r["reasons"]
            )
        )
        self.assertIn("MISMATCH", self.report(self.doc))

    def test_a_new_block_cannot_pass_as_an_old_one_or_the_reverse(self):
        legacy = copy.deepcopy(self.doc)
        legacy["prefix_cache_probe"].update(
            agent_turn=legacy["prefix_cache_probe"]["repeat"]
        )
        with self.assertRaises(ValueError):
            validate(legacy)
        current = copy.deepcopy(self.doc)
        probe = current["prefix_cache_probe"]
        del probe["extension"]
        with self.assertRaises(ValueError):
            validate(current)


if __name__ == "__main__":
    unittest.main()

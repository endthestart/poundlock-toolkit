"""CLI glue that the module tests skip.

`recommend --format json` writes the summary Poundlock's capability converter
(`scripts/toolkit-capabilities.py` in the Poundlock repository) reads instead of
importing Toolkit, so these tests pin every field it uses. `recover` and
`normalize-legacy` are documented recovery paths no other test runs through the CLI.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest

from poundlock_toolkit import __version__
from poundlock_toolkit.cli import EXIT_INVALID, EXIT_OK, main
from poundlock_toolkit.core import config, digest, read_json, validate
from poundlock_toolkit.evaluation import attempt, evaluate, new_document
from poundlock_toolkit.reporting import WORKLOADS, export, recommendation
from poundlock_toolkit.schema_evaluation import evaluate as schema_evaluate

from test_investigation import ToolCapableBackend, fake_factory
from test_tool_schema import calls, valid_arguments

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "src/poundlock_toolkit/data/examples"
SUMMARY = "urn:poundlock-toolkit:recommendation:1"

#: The levels the converter maps to a capability status; any other stops it.
LEVELS = {"repeated task qualification", "observed smoke pass", "insufficient evidence"}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


class RecommendJsonTests(CliCase):
    def results(self):
        """One result of each kind the converter reads, saved where evaluate
        writes them: a coding run, both investigation variants and a
        tool-schema run on one boundary (the converter refuses a run whose
        boundaries sent different thinking settings)."""
        coding = evaluate(
            config(EXAMPLES / "pi.json"),
            self.root / "coding.json",
            backend=ToolCapableBackend(),
            pi_factory=fake_factory(),
        )
        variants = []
        for variant in ("findable", "absent"):
            c = config(EXAMPLES / "investigation.json")
            c["investigation"]["variant"] = variant
            variants.append(
                evaluate(
                    c,
                    self.root / f"{variant}.json",
                    backend=ToolCapableBackend(),
                    pi_factory=fake_factory(),
                )
            )
        c = config(EXAMPLES / "tool-schema.json")
        c["limits"].update(total_seconds=10, request_seconds=2)
        del c["tool_schema"]["endpoints"]["ollama"]
        tool_schema = schema_evaluate(
            c,
            self.root / "tool-schema.json",
            requester=lambda c, boundary: (calls(valid_arguments()), True, None),
        )
        return coding, *variants, tool_schema

    def test_summary_carries_every_field_the_capability_converter_reads(self):
        coding, findable, absent, tool_schema = documents = self.results()
        out = self.root / "summary.json"
        code, _, _ = self.cli(
            "recommend",
            *(self.root / f"{n}.json" for n in ("coding", "findable", "absent")),
            self.root / "tool-schema.json",
            "--format",
            "json",
            "--output",
            out,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.mode(out), 0o600)
        value = json.loads(out.read_text())
        validate(value, "recommendation.schema.json")
        self.assertEqual(value["schema"], SUMMARY)

        rows = {row["run_id"]: row for row in value["results"]}
        self.assertEqual(set(rows), {d["run_id"] for d in documents})
        for d in documents:
            row = rows[d["run_id"]]
            self.assertEqual(row["result_schema"], d["schema"])
            self.assertEqual(row["result_sha256"], digest(export(d)))
            self.assertEqual(row["identity_sha256"], d["identity_sha256"])
            self.assertEqual(row["created_at"], d["created_at"])
            self.assertEqual(row["toolkit_version"], __version__)
            for key in ("context_tokens", "max_output_tokens"):
                self.assertEqual(row["settings"][key], d["settings"][key])

        # Qualification results: the coding verdict and what reached the server
        # about thinking (reasoning off on the OpenAI boundary omits the field).
        for d in (coding, findable, absent):
            row = rows[d["run_id"]]
            self.assertEqual(row["harness"], d["harness"])
            self.assertEqual(row["settings"]["reasoning_sent"], "omitted")
            [verdict] = [w for w in row["workloads"] if w["workload"] == "coding"]
            self.assertIn(verdict["level"], LEVELS)
            self.assertLessEqual(verdict["passed"], verdict["attempted"])
        [verdict] = [
            w for w in rows[coding["run_id"]]["workloads"] if w["workload"] == "coding"
        ]
        self.assertGreater(verdict["planned"], 0)
        self.assertEqual(verdict["level"], recommendation(coding, "coding")["level"])

        # The tool-schema result: its mode, and its boundary's counts and thinking.
        row = rows[tool_schema["run_id"]]
        self.assertEqual(row["harness"], "direct")
        self.assertEqual(row["tool_schema"]["mode"], tool_schema["contract"]["mode"])
        [boundary] = row["tool_schema"]["boundaries"]
        self.assertEqual(boundary["boundary"], "openai")
        for key in ("planned", "attempted", "passed"):
            self.assertEqual(boundary[key], tool_schema["summary"]["openai"][key])
        self.assertEqual(boundary["reasoning_sent"], "omitted")

        # The investigation pair, with each variant's verdict.
        [pair] = value["investigation_pairs"]
        self.assertEqual(pair["findable_run_id"], findable["run_id"])
        self.assertEqual(pair["absent_run_id"], absent["run_id"])
        self.assertIsInstance(pair["qualified"], bool)
        for variant in ("findable", "absent"):
            self.assertIn(pair[variant]["level"], LEVELS)
            self.assertLessEqual(pair[variant]["passed"], pair[variant]["attempted"])

    def test_without_output_the_summary_goes_to_stdout(self):
        for argv in (
            ("example", "openai", "--output", self.root / "openai.json"),
            ("demo", self.root / "openai.json", "--output", self.root / "demo.json"),
        ):
            self.assertEqual(self.cli(*argv)[0], EXIT_OK)
        code, out, _ = self.cli(
            "recommend", self.root / "demo.json", "--format", "json"
        )
        self.assertEqual(code, EXIT_OK)
        value = json.loads(out)
        self.assertEqual(out, json.dumps(value, indent=2, sort_keys=True) + "\n")
        validate(value, "recommendation.schema.json")
        [row] = value["results"]
        self.assertTrue(row["synthetic"])

    def test_an_invalid_result_writes_no_summary(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"schema": "urn:poundlock-toolkit:qualification:1"}))
        out = self.root / "summary.json"
        code, _, _ = self.cli("recommend", bad, "--format", "json", "--output", out)
        self.assertEqual(code, EXIT_INVALID)
        self.assertFalse(out.exists())


class VersionTests(unittest.TestCase):
    def test_the_package_reports_the_version_pyproject_releases(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            declared = tomllib.load(handle)["project"]["version"]
        self.assertEqual(__version__, declared)


class RecoverTests(CliCase):
    def interrupted_run(self):
        d = new_document(config(EXAMPLES / "openai.json"))
        d["attempts"] = [attempt("readiness", "readiness")]
        path = self.root / "run.json"
        path.write_text(json.dumps(d))
        return d, path

    def test_running_attempts_become_interrupted_and_the_source_is_kept(self):
        d, source = self.interrupted_run()
        before = source.read_bytes()
        out = self.root / "recovered.json"
        code, _, _ = self.cli("recover", source, "--output", out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.mode(out), 0o600)
        recovered = validate(read_json(out))
        self.assertEqual(recovered["run_id"], d["run_id"])
        self.assertEqual(recovered["attempts"][0]["outcome"], "interrupted")
        self.assertEqual(
            recovered["attempts"][0]["interventions"], ["recovered_after_interruption"]
        )

    def test_an_existing_output_is_never_overwritten(self):
        _, source = self.interrupted_run()
        out = self.root / "recovered.json"
        out.write_text("keep")
        code, _, _ = self.cli("recover", source, "--output", out)
        self.assertEqual(code, EXIT_INVALID)
        self.assertEqual(out.read_text(), "keep")


def legacy_result():
    """A minimal document in the pre-Toolkit result schema (legacy.schema.json)."""
    return {
        "schema_version": 1,
        "run_id": "legacy-run",
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        },
        "machine": {
            "machine_class": "workstation",
            "os": "linux",
            "architecture": "x86_64",
            "memory": {
                "architecture": "discrete",
                "system_ram_bytes": None,
                "unified_memory_bytes": None,
                "gpu_vram_pools_bytes": [],
            },
        },
        "backend": {
            "name": "llama.cpp",
            "version": "b1",
            "source": "https://github.com/ggml-org/llama.cpp",
            "api_dialect": "openai",
            "configuration": {
                "prefix_cache": "unknown",
                "weight_residency": "gpu",
                "model_idle_unload_seconds": None,
            },
        },
        "model": {
            "repository": "example/model",
            "revision": None,
            "format": "gguf",
            "weight_quantization": "Q4_K_M",
            "artifact_bytes": 0,
            "artifact_sha256": None,
        },
        "workload": {
            "name": "extraction",
            "prompt_sha256": "0" * 64,
            "context_window_tokens": 8192,
            "sampling": {},
            "cache_state": "unknown",
        },
        "measurements": {
            "wall_seconds": 1.0,
            "ttft_seconds": None,
            "decode_tokens_per_second": None,
        },
        "verdict": {"classification": "pass", "objective_pass": True, "notes": []},
        "provenance": {
            "framework_commit": "0123456",
            "runner": "manual",
            "raw_evidence_sha256": None,
        },
    }


class NormalizeLegacyTests(CliCase):
    def test_original_bytes_are_kept_and_the_envelope_never_qualifies(self):
        # Indented differently from Toolkit's own output, so a rewrite would show.
        raw = (json.dumps(legacy_result(), indent=1) + "\n").encode()
        source = self.root / "legacy.json"
        source.write_bytes(raw)
        out = self.root / "normalized"
        code, _, _ = self.cli(
            "normalize-legacy",
            source,
            "--config",
            EXAMPLES / "openai.json",
            "--output",
            out,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.mode(out), 0o700)
        self.assertEqual((out / "original.json").read_bytes(), raw)
        self.assertEqual(self.mode(out / "original.json"), 0o600)
        envelope = validate(read_json(out / "envelope.json"))
        self.assertEqual(envelope["legacy_sha256"], digest(raw))
        self.assertFalse(envelope["identity_complete"])
        for workload in WORKLOADS:
            self.assertEqual(
                recommendation(envelope, workload)["level"], "insufficient evidence"
            )

    def test_a_document_outside_the_legacy_schema_is_refused(self):
        source = self.root / "legacy.json"
        broken = legacy_result()
        del broken["provenance"]
        source.write_text(json.dumps(broken))
        out = self.root / "normalized"
        code, _, _ = self.cli(
            "normalize-legacy",
            source,
            "--config",
            EXAMPLES / "openai.json",
            "--output",
            out,
        )
        self.assertEqual(code, EXIT_INVALID)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()

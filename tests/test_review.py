"""(f) Blind review: anonymised packet, one rubric, a separately kept key."""

import contextlib
import io
import json
import os
from pathlib import Path
import random
import stat
import tempfile
import unittest

from poundlock_toolkit.cli import EXIT_INVALID, main
from poundlock_toolkit.review import build_packet, key_inside_packet, read_candidates

SECRET = "internal-branch-name-must-not-leak"


class ReviewPacketTests(unittest.TestCase):
    def candidates(self):
        return [
            {"identity": "trial-a", "diff": "diff --git a/x.py\n+a\n"},
            {"identity": SECRET, "diff": "diff --git a/y.py\n+b\n"},
            {"identity": "trial-c", "diff": "diff --git a/z.py\n+c\n"},
        ]

    def test_labels_are_opaque_and_key_maps_them_back(self):
        packet = build_packet(self.candidates(), rng=random.Random(0))
        self.assertEqual(
            set(packet["key"]), {"Candidate A", "Candidate B", "Candidate C"}
        )
        self.assertEqual(set(packet["key"].values()), {"trial-a", SECRET, "trial-c"})
        # The packet a reviewer sees never carries the real identity.
        self.assertNotIn(SECRET, packet["packet"])
        self.assertNotIn("trial-a", packet["packet"])
        for label in packet["key"]:
            self.assertIn(label, packet["packet"])

    def test_order_is_shuffled_not_input_order(self):
        # With this seed the shuffled order differs from input order; the
        # point under test is that build_packet consults `rng`, not a fixed
        # A-to-first mapping.
        ordered = build_packet(self.candidates(), rng=random.Random(0))
        reordered = build_packet(
            list(reversed(self.candidates())), rng=random.Random(0)
        )
        self.assertNotEqual(ordered["key"], reordered["key"])

    def test_rubric_uses_the_three_verdicts_and_warns_against_self_review(self):
        packet = build_packet(self.candidates(), rng=random.Random(0))
        for verdict in ("merge as-is", "merge after small fix", "rewrite"):
            self.assertIn(verdict, packet["rubric"])
        self.assertIn("independent review", packet["rubric"].lower())

    def test_candidate_bounds_and_unique_identities(self):
        with self.assertRaises(ValueError):
            build_packet([{"identity": "solo", "diff": "x"}])
        dup = [
            {"identity": "same", "diff": "a"},
            {"identity": "same", "diff": "b"},
        ]
        with self.assertRaises(ValueError):
            build_packet(dup)

    def test_read_candidates_loads_diff_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.diff").write_text("diff --git a/x.py\n+a\n")
            (root / "b.diff").write_text("diff --git a/y.py\n+b\n")
            descriptor = {
                "candidates": [
                    {"identity": "one", "diff_path": str(root / "a.diff")},
                    {"identity": "two", "diff_path": str(root / "b.diff")},
                ]
            }
            loaded = read_candidates(descriptor)
            self.assertEqual({c["identity"] for c in loaded}, {"one", "two"})
            self.assertTrue(all("diff --git" in c["diff"] for c in loaded))


class KeyPlacementTests(unittest.TestCase):
    def test_inside_means_the_folder_itself_or_anything_beneath_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            packet = root / "packet"
            for key, inside in [
                (packet, True),
                (packet / "key.json", True),
                (packet / "deeper" / "key.json", True),
                (packet / ".." / "packet" / "key.json", True),
                (root / "key.json", False),
                (root / "packet-key.json", False),
                (root / "packet2" / "key.json", False),
                (packet / ".." / "key.json", False),
            ]:
                with self.subTest(key=key.relative_to(root)):
                    self.assertEqual(key_inside_packet(packet, key), inside)

    def test_a_symlink_into_the_packet_does_not_hide_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "packet").mkdir()
            (root / "shortcut").symlink_to(root / "packet", target_is_directory=True)
            self.assertTrue(
                key_inside_packet(root / "packet", root / "shortcut" / "key.json")
            )

    def test_relative_paths_are_compared_by_where_they_point(self):
        with tempfile.TemporaryDirectory() as folder:
            cwd = os.getcwd()
            os.chdir(folder)
            try:
                self.assertTrue(key_inside_packet("packet", "packet/key.json"))
                self.assertTrue(key_inside_packet("./packet", "packet/../packet/key"))
                self.assertFalse(key_inside_packet("packet", "key.json"))
            finally:
                os.chdir(cwd)


class ReviewPacketCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in argv])
        self.stderr = err.getvalue()
        return code, out.getvalue()

    def descriptor(self):
        (self.root / "a.diff").write_text("+a\n")
        (self.root / "b.diff").write_text("+b\n")
        path = self.root / "candidates.json"
        path.write_text(
            json.dumps(
                {
                    "candidates": [
                        {"identity": "one", "diff_path": str(self.root / "a.diff")},
                        {"identity": "two", "diff_path": str(self.root / "b.diff")},
                    ]
                }
            )
        )
        return path

    def test_cli_writes_packet_rubric_and_separate_key(self):
        (self.root / "a.diff").write_text("diff --git a/x.py\n+a\n" + SECRET)
        (self.root / "b.diff").write_text("diff --git a/y.py\n+b\n")
        descriptor = self.root / "candidates.json"
        descriptor.write_text(
            json.dumps(
                {
                    "candidates": [
                        {"identity": SECRET, "diff_path": str(self.root / "a.diff")},
                        {"identity": "trial-b", "diff_path": str(self.root / "b.diff")},
                    ]
                }
            )
        )
        out_dir, key_out = self.root / "packet", self.root / "key.json"
        code, printed = self.cli(
            "review-packet", descriptor, "--output", out_dir, "--key-out", key_out
        )
        self.assertEqual(code, 0)
        self.assertIn("key kept separately", printed)
        # The folder the reviewer is handed holds the packet and rubric only.
        self.assertEqual(
            sorted(p.name for p in out_dir.iterdir()), ["RUBRIC.md", "packet.md"]
        )
        self.assertEqual(stat.S_IMODE(key_out.stat().st_mode), 0o600)
        packet_text = (out_dir / "packet.md").read_text()
        rubric_text = (out_dir / "RUBRIC.md").read_text()
        key = json.loads(key_out.read_text())
        self.assertIn("merge after small fix", rubric_text)
        self.assertEqual(set(key.values()), {SECRET, "trial-b"})
        # SECRET appears once, inside the diff content itself -- never as a
        # label or anywhere the real identity would leak through naming.
        self.assertEqual(packet_text.count(SECRET), 1)
        self.assertNotIn("trial-b", packet_text)

    def test_cli_refuses_to_overwrite_existing_output(self):
        descriptor = self.descriptor()
        out_dir, key_out = self.root / "packet", self.root / "key.json"
        self.assertEqual(
            self.cli(
                "review-packet", descriptor, "--output", out_dir, "--key-out", key_out
            )[0],
            0,
        )
        key_before = key_out.read_text()
        code, _ = self.cli(
            "review-packet",
            descriptor,
            "--output",
            self.root / "second",
            "--key-out",
            key_out,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(key_out.read_text(), key_before)
        self.assertFalse((self.root / "second").exists())
        code, _ = self.cli(
            "review-packet",
            descriptor,
            "--output",
            out_dir,
            "--key-out",
            self.root / "other-key.json",
        )
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "other-key.json").exists())

    def test_key_inside_the_packet_folder_is_refused_and_nothing_is_written(self):
        descriptor = self.descriptor()
        out_dir = self.root / "packet"
        # A dangling symlink that will lead into the folder once it is created.
        (self.root / "shortcut").symlink_to(out_dir, target_is_directory=True)
        for key_out in (
            out_dir / "key.json",
            out_dir / "nested" / "key.json",
            out_dir / ".." / "packet" / "key.json",
            self.root / "shortcut" / "key.json",
            out_dir,
        ):
            with self.subTest(key_out=key_out.relative_to(self.root)):
                code, printed = self.cli(
                    "review-packet",
                    descriptor,
                    "--output",
                    out_dir,
                    "--key-out",
                    key_out,
                )
                self.assertEqual(code, EXIT_INVALID)
                self.assertEqual(printed, "")
                self.assertIn("--key-out must be outside", self.stderr)
                self.assertFalse(out_dir.exists())

    def test_key_next_to_the_packet_folder_is_allowed_even_with_a_similar_name(self):
        descriptor = self.descriptor()
        out_dir, key_out = self.root / "packet", self.root / "packet-key.json"
        code, _ = self.cli(
            "review-packet", descriptor, "--output", out_dir, "--key-out", key_out
        )
        self.assertEqual(code, 0)
        self.assertTrue(key_out.is_file())
        self.assertNotIn(key_out.name, [p.name for p in out_dir.iterdir()])

    def test_key_out_is_required(self):
        descriptor = self.descriptor()
        with self.assertRaises(SystemExit) as caught:
            self.cli("review-packet", descriptor, "--output", self.root / "packet")
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.root / "packet").exists())

    def test_an_unusable_key_path_leaves_no_packet_behind(self):
        descriptor = self.descriptor()
        out_dir = self.root / "packet"
        (self.root / "taken.json").write_text("precious")
        for key_out in (
            self.root / "missing-folder" / "key.json",
            self.root / "taken.json",
        ):
            with self.subTest(key_out=key_out.relative_to(self.root)):
                code, _ = self.cli(
                    "review-packet",
                    descriptor,
                    "--output",
                    out_dir,
                    "--key-out",
                    key_out,
                )
                self.assertEqual(code, EXIT_INVALID)
                self.assertFalse(out_dir.exists())
        self.assertEqual((self.root / "taken.json").read_text(), "precious")

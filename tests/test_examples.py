import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from poundlock_toolkit.cli import EXIT_INVALID, EXIT_OK, main
from poundlock_toolkit.core import config
from poundlock_toolkit.examples import names, text

ROOT = Path(__file__).resolve().parents[1]


class PackagedExampleTests(unittest.TestCase):
    """A wheel install has no examples/ folder; the CLI writes the packaged ones."""

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

    def test_every_documented_example_is_packaged(self):
        for name in (
            "openai",
            "ollama",
            "pi",
            "investigation",
            "tool-schema",
            "standalone",
            "target-local",
            "target-ssh",
        ):
            self.assertIn(name, names())

    def test_no_packaged_example_is_a_result(self):
        # `example` offers configurations to edit; a result document (a schema URN
        # and attempts) would be written out as if it were one.
        for name in names():
            document = json.loads(text(name))
            self.assertNotIn("attempts", document, name)
            self.assertFalse(
                str(document.get("schema", "")).startswith("urn:poundlock-toolkit:"),
                name,
            )

    def test_listing_names_every_example(self):
        code, out, _ = self.cli("example")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out.split(), names())

    def test_written_example_is_a_private_copy_that_loads(self):
        target = self.root / "openai.json"
        code, _, _ = self.cli("example", "openai", "--output", target)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        packaged = ROOT / "src/poundlock_toolkit/data/examples/openai.json"
        self.assertEqual(
            json.loads(target.read_text()), json.loads(packaged.read_text())
        )
        config(target)

    def test_existing_output_is_never_overwritten(self):
        target = self.root / "mine.json"
        target.write_text("keep")
        code, _, _ = self.cli("example", "openai", "--output", target)
        self.assertEqual(code, EXIT_INVALID)
        self.assertEqual(target.read_text(), "keep")

    def test_unknown_name_lists_the_choices(self):
        code, _, err = self.cli("example", "nope", "--output", self.root / "x.json")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("openai", err)
        self.assertFalse((self.root / "x.json").exists())

    def test_a_name_without_output_is_refused(self):
        code, _, err = self.cli("example", "openai")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("--output", err)


if __name__ == "__main__":
    unittest.main()

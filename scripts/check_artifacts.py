"""Check and install the built artifacts, never the editable source tree."""

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def run(argv, cwd):
    subprocess.run([str(x) for x in argv], cwd=cwd, check=True, timeout=120)


def main():
    files = sorted(DIST.glob("*.whl")) + sorted(DIST.glob("*.tar.gz"))
    if len(files) != 2:
        raise SystemExit("Expected exactly one wheel and one sdist")
    for artifact in files:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
        else:
            with tarfile.open(artifact) as archive:
                names = archive.getnames()
        for suffix in (
            "data/config.schema.json",
            "data/qualification.schema.json",
            "data/legacy.schema.json",
            "data/propose-plan.schema.json",
            "data/tool-schema-result.schema.json",
            "data/tool-schema-smoke.json",
            "data/tool-schema-qualification.json",
            "data/verify.py",
            "data/verify_investigation.py",
            "data/pi-policy.ts",
            "data/examples/openai.json",
            "data/investigation/findable/answer-key.json",
            "data/investigation/findable/corpus/modules/epsilon-9.md",
            "data/investigation/absent/answer-key.json",
            "data/investigation/absent/corpus/index/capability-index.md",
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY.md",
        ):
            assert any(n.endswith(suffix) for n in names), (artifact.name, suffix)
        with tempfile.TemporaryDirectory(prefix="toolkit-install-") as tmp:
            cwd = Path(tmp)
            env = cwd / "venv"
            venv.create(env, with_pip=True)
            python = env / "bin/python"
            run([python, "-m", "pip", "install", artifact], cwd)
            run([python, "-I", "-m", "poundlock_toolkit", "--help"], cwd)
            run([python, "-I", "-m", "poundlock_toolkit", "inspect"], cwd)
            # The README's offline quickstart, verbatim, from a folder with no
            # checkout: every input comes from the installed package.
            toolkit = [python, "-I", "-m", "poundlock_toolkit"]
            for name in ("openai", "tool-schema", "investigation"):
                run([*toolkit, "example", name, "--output", f"{name}.json"], cwd)
            run([*toolkit, "plan", "openai.json", "--output", "plan.json"], cwd)
            run([*toolkit, "demo", "openai.json", "--output", "demo.json"], cwd)
            run([*toolkit, "recommend", "demo.json", "--output", "report.md"], cwd)
            run(
                [
                    python,
                    "-I",
                    "-m",
                    "poundlock_toolkit",
                    "export",
                    "demo.json",
                    "--output",
                    "qualification.json",
                ],
                cwd,
            )
            run(
                [
                    python,
                    "-I",
                    "-m",
                    "poundlock_toolkit",
                    "validate",
                    "qualification.json",
                ],
                cwd,
            )
            run(
                [
                    python,
                    "-I",
                    "-m",
                    "poundlock_toolkit",
                    "plan",
                    "tool-schema.json",
                    "--output",
                    "schema-plan.json",
                ],
                cwd,
            )
            run(
                [
                    python,
                    "-I",
                    "-c",
                    "from poundlock_toolkit.tool_schema import score; assert not score({'tasks': '[]'})['schema_valid']",
                ],
                cwd,
            )
            # Proves the nested investigation corpus (not a flat data/* file) was
            # actually bundled and loads from the installed artifact, not the
            # editable source tree.
            run(
                [
                    python,
                    "-I",
                    "-m",
                    "poundlock_toolkit",
                    "plan",
                    "investigation.json",
                    "--output",
                    "investigation-plan.json",
                ],
                cwd,
            )
            run(
                [
                    python,
                    "-I",
                    "-c",
                    "from poundlock_toolkit.fixtures import INVESTIGATION_FIXTURE; "
                    "fx = INVESTIGATION_FIXTURE('absent'); "
                    "assert 'answer-key.json' not in fx.worker_files; "
                    "assert fx.entry_count == 4",
                ],
                cwd,
            )
            # Exercise a real isolated target process using the installed artifact.
            (cwd / "target.json").write_text(
                json.dumps({"transport": "local", "host": None, "python": str(python)})
            )
            run(
                [
                    python,
                    "-I",
                    "-m",
                    "poundlock_toolkit",
                    "target-inspect",
                    "target.json",
                    "--output",
                    "inventory.json",
                ],
                cwd,
            )
    (DIST / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files
        )
    )
    print("Wheel and sdist independently installed and exercised")


if __name__ == "__main__":
    main()

"""Thin CLI presentation; evaluation and recommendation remain importable."""

import argparse
import json
import os
import sys
from pathlib import Path
from . import __version__
from .core import Failure, atomic_json, config, digest, read_json, validate
from .drift import DriftError, evaluation_check, require
from .evaluation import evaluate, new_document, recover
from .inspection import inspect
from .planning import plan
from .reporting import export, report
from .review import build_packet, key_inside_packet, read_candidates


EXIT_OK = 0
EXIT_EVAL_FAILED = 1
EXIT_INVALID = 2
EXIT_CONSENT = 3
EXIT_DRIFT = 4
EXIT_CANCELLED = 130


def parser():
    p = argparse.ArgumentParser(
        description="Bounded local/SSH qualification and reviewed standalone setup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            f"  {EXIT_OK}    command or selected attempts succeeded\n"
            f"  {EXIT_EVAL_FAILED}    evaluation contains a failure or skip\n"
            f"  {EXIT_INVALID}    invalid input, unmet prerequisites, or a plan with blockers\n"
            f"  {EXIT_CONSENT}    pending consent (evaluate without --execute)\n"
            f"  {EXIT_DRIFT}    a reviewed assumption drifted or an attach check failed;\n"
            "       the output names the check and the next required step\n"
            f"  {EXIT_CANCELLED}  cancelled"
        ),
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    from .target_cli import arguments

    arguments(sub)
    from .setup import arguments as setup_arguments

    setup_arguments(sub)
    sub.add_parser("inspect", help="Read-only local inventory and unknowns")
    s = sub.add_parser(
        "example",
        help="Write a packaged example configuration to edit; list them without a name",
    )
    s.add_argument("name", nargs="?")
    s.add_argument("--output")
    s = sub.add_parser(
        "plan", help="Show a bounded evaluation plan, without network calls"
    )
    s.add_argument("config")
    s.add_argument("--output", required=True)
    s.add_argument(
        "--readiness-only",
        action="store_true",
        help="Only read model discovery; no inference",
    )
    s = sub.add_parser("evaluate", help="Execute exactly the reviewed configuration")
    s.add_argument("config")
    s.add_argument("--plan", required=True)
    s.add_argument("--execute", action="store_true")
    s.add_argument("--output", required=True)
    s.add_argument("--readiness-only", action="store_true")
    s = sub.add_parser(
        "demo", help="Offline synthetic workflow, never hardware qualification"
    )
    s.add_argument("config")
    s.add_argument("--output", required=True)
    s = sub.add_parser(
        "recommend", help="Explain per-workload evidence and limitations"
    )
    s.add_argument("results", nargs="+")
    s.add_argument("--output")
    s.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="json writes the versioned summary other programs read (urn:poundlock-toolkit:recommendation:1)",
    )
    for name in ("export", "recover"):
        s = sub.add_parser(name)
        s.add_argument("result")
        s.add_argument("--output", required=True)
    s = sub.add_parser("validate")
    s.add_argument("result")
    s = sub.add_parser(
        "normalize-legacy",
        help="Preserve original bytes and create a non-qualifying envelope",
    )
    s.add_argument("result")
    s.add_argument("--config", required=True)
    s.add_argument("--output", required=True)
    s = sub.add_parser(
        "review-packet",
        help="Read-only, network-free: export an anonymised blind-review packet and rubric, and write the key to a separate file",
    )
    s.add_argument("candidates")
    s.add_argument("--output", required=True)
    s.add_argument(
        "--key-out",
        required=True,
        help="Where to write the label-to-identity key. It must be outside the --output folder, and is never shared with the reviewer",
    )
    return p


def fresh(path):
    if Path(path).exists():
        raise ValueError("Output already exists")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command.startswith("setup-"):
            from .setup import cli

            return cli(args)
        if args.command.startswith("target-"):
            from .target_cli import run

            return run(args)
        if args.command == "inspect":
            print(json.dumps(inspect(), indent=2))
        elif args.command == "example":
            from .examples import names, text

            if args.name is None:
                print("\n".join(names()))
                return EXIT_OK
            if args.name not in names():
                print(
                    "Unknown example. Available: " + ", ".join(names()),
                    file=sys.stderr,
                )
                return EXIT_INVALID
            if not args.output:
                print("example NAME needs --output FILE", file=sys.stderr)
                return EXIT_INVALID
            fresh(args.output)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text(args.name))
            print(
                f"Wrote the {args.name} example to {args.output}; edit it before use."
            )
        elif args.command == "plan":
            c = config(args.config)
            result = plan(c, args.readiness_only)
            fresh(args.output)
            atomic_json(args.output, result)
            print(json.dumps(result, indent=2))
            return EXIT_INVALID if result["blockers"] else EXIT_OK
        elif args.command in ("evaluate", "demo"):
            c = config(args.config)
            if args.command == "evaluate":
                reviewed = read_json(args.plan)
                require([evaluation_check(reviewed, plan(c, args.readiness_only))])
                print(json.dumps(reviewed, indent=2), flush=True)
                if not args.execute:
                    print("Plan only. Add --execute to run this bounded evaluation.")
                    return EXIT_CONSENT
                d = evaluate(
                    c,
                    args.output,
                    readiness_only=args.readiness_only,
                    progress=lambda s, w, r: print(
                        f"{s}: {w}, repetition {r}", flush=True
                    ),
                )
            else:
                from .demo import SyntheticBackend

                # Synthetic flag is recorded before the first attempt, including interruption.
                d = evaluate(c, args.output, backend=SyntheticBackend(), synthetic=True)
            print(report([d]))
            return (
                EXIT_OK
                if all(a["outcome"] == "pass" for a in d["attempts"])
                else EXIT_EVAL_FAILED
            )
        elif args.command == "recommend":
            documents = [read_json(p) for p in args.results]
            if args.format == "json":
                from .reporting import summary

                text = json.dumps(summary(documents), indent=2, sort_keys=True) + "\n"
            else:
                text = report(documents)
            if args.output:
                fresh(args.output)
                fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(text)
            else:
                print(text, end="")
        elif args.command in ("export", "recover"):
            fresh(args.output)
            original = read_json(args.result)
            result = export(original) if args.command == "export" else recover(original)
            atomic_json(args.output, result)
            print("Validated document written; source preserved.")
        elif args.command == "validate":
            validate(read_json(args.result))
            print("Schema: valid")
        elif args.command == "normalize-legacy":
            raw = Path(args.result).read_bytes()
            validate(json.loads(raw), "legacy.schema.json")
            fresh(args.output)
            root = Path(args.output)
            root.mkdir(mode=0o700)
            fd = os.open(
                root / "original.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            d = new_document(config(args.config))
            d.update(identity_complete=False, legacy_sha256=digest(raw))
            atomic_json(root / "envelope.json", validate(d))
            print(
                "Original bytes preserved privately. Envelope is non-qualifying provenance only."
            )
        elif args.command == "review-packet":
            if key_inside_packet(args.output, args.key_out):
                print(
                    "--key-out must be outside the --output folder: the reviewer is handed that folder and must never receive the key.",
                    file=sys.stderr,
                )
                return EXIT_INVALID
            packet = build_packet(read_candidates(read_json(args.candidates)))
            # Refuse before writing anything, so a bad path leaves no packet behind
            # without its key, or a key without its packet.
            for target in (args.output, args.key_out):
                fresh(target)
                if not Path(target).parent.is_dir():
                    raise ValueError("Parent folder does not exist")
            root = Path(args.output)
            root.mkdir(mode=0o700)
            for path, content in (
                (
                    Path(args.key_out),
                    json.dumps(packet["key"], indent=2, sort_keys=True) + "\n",
                ),
                (root / "packet.md", packet["packet"]),
                (root / "RUBRIC.md", packet["rubric"]),
            ):
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(content)
            print(
                f"Packet and rubric written for the reviewer; key kept separately in {args.key_out} -- do not share it with the reviewer."
            )
        return EXIT_OK
    except DriftError as drift:
        print(json.dumps(drift.report(), indent=2), flush=True)
        return EXIT_DRIFT
    except KeyboardInterrupt:
        print(
            "Cancelled; retained attempt evidence can be inspected or recovered.",
            file=sys.stderr,
        )
        return EXIT_CANCELLED
    except (Failure, ValueError, OSError, KeyError, TypeError, RuntimeError):
        print(
            "Operation failed. Check schema, matching plan, new output path and prerequisites. Private error details were suppressed.",
            file=sys.stderr,
        )
        return EXIT_INVALID

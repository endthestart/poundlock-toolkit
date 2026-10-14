"""Pi fixture data: what is mounted where, and what counts as a pass.

A `PiFixture` separates files the worker container may read (`worker_files`,
mounted read-only at `/fixture`, plus `starter_outputs` seeded into the
writable `/work`) from files that exist only for the second, no-network
verifier container (`verifier_files`). For `investigation`, the answer key
lives exclusively in `verifier_files` -- it is never part of the worker
mount, so the worker container cannot read it under any tool policy.

`pi.py` is generic over `PiFixture`; only this module knows the concrete
starter file, verifier command, output allowlist and expected verifier
stdout for each workload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from .core import data, data_tree
from .workloads import CODING, INVESTIGATION

VERIFIER_COMMAND = "python3 -I /fixture/verify.py /work/slugify.py"
STARTER = 'def slugify(text: str) -> str:\n    return text.lower().replace(" ", "-")\n'
CODING_REPORT_CONTRACT = (
    "Report contract, restated because your conversation history may have been "
    'summarized: you must still finish by running exactly "python3 -I '
    '/fixture/verify.py /work/slugify.py", then writing RESULT.json with '
    'exactly {"status":"passed","verifier_exit_code":0} only if that verifier '
    "passed, or the analogous failed status otherwise. Do this even if your "
    "own summary of earlier progress does not mention it."
)


@dataclass(frozen=True)
class PiFixture:
    name: str
    prompt: str
    # Copied into the writable /work before the agent starts; () for none.
    starter_outputs: dict
    # Mounted read-only at /fixture; the only paths the "read" tool may open
    # besides the writable outputs below.
    worker_files: dict
    # Mounted read-only only into the second, no-network verifier container.
    # Never exposed to the worker under any policy.
    verifier_files: dict
    verify_entry: str
    candidate_output: str
    candidate_name: str
    verify_args: tuple
    allowed_outputs: tuple
    verifier_command: str | None
    expected_verifier_stdout: bytes
    completion: Callable[[bytes, bytes], bool]
    # Pi tools the worker is offered. Only grant `bash` where some command is
    # actually permitted: a fixture with no protected verifier command denies
    # every shell command, and a model that reaches for the shell then burns
    # its whole turn budget on refusals.
    tools: str = "read,write,edit,bash"
    result_filename: str = "RESULT.json"
    variant: str | None = None
    corpus_sha256: str | None = None
    entry_count: int | None = None
    # Restated in the system prompt only when the compaction gate is on
    # (see PiRunner.run): compaction summarizes conversation history but
    # never the system prompt, so a contract placed there survives it.
    report_contract: str | None = None

    def verify_argv(self):
        return [f"/fixture/{self.verify_entry}", f"/fixture/{self.candidate_name}"] + [
            f"/fixture/{a}" for a in self.verify_args
        ]


def _coding_completion(events, result):
    invoked, terminal, tools = False, False, 0
    try:
        for line in events.splitlines():
            event = json.loads(line)
            if event.get("type") == "tool_execution_start":
                tools += 1
                args = event.get("args") or {}
                if (
                    event.get("toolName") == "bash"
                    and args.get("command", "").strip() == VERIFIER_COMMAND
                ):
                    invoked = True
            if event.get("type") == "agent_end":
                messages = event.get("messages") or []
                terminal = any(
                    m.get("role") == "assistant" and m.get("stopReason") == "stop"
                    for m in messages
                )
        record = json.loads(result)
        return (
            invoked
            and terminal
            and tools > 0
            and record == {"status": "passed", "verifier_exit_code": 0}
            and type(record["verifier_exit_code"]) is int
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _investigation_completion(events, result):
    """Self-report only: a plausible shape and a normal stop. Not correctness --
    correctness is decided independently by verify_investigation.py against the
    answer key, which this predicate never sees."""
    terminal, tools = False, 0
    try:
        for line in events.splitlines():
            event = json.loads(line)
            if event.get("type") == "tool_execution_start":
                tools += 1
            if event.get("type") == "agent_end":
                messages = event.get("messages") or []
                terminal = any(
                    m.get("role") == "assistant" and m.get("stopReason") == "stop"
                    for m in messages
                )
        record = json.loads(result)
        if not (
            isinstance(record, dict)
            and record.keys() == {"verdict", "entry", "evidence_path", "evidence_line"}
        ):
            return False
        if record["verdict"] not in ("satisfied", "not_satisfied"):
            return False
        if record["verdict"] == "not_satisfied":
            shape_ok = (
                record["entry"] is None
                and record["evidence_path"] is None
                and record["evidence_line"] is None
            )
        else:
            shape_ok = (
                isinstance(record["entry"], str)
                and record["entry"] != ""
                and isinstance(record["evidence_path"], str)
                and record["evidence_path"] != ""
                and type(record["evidence_line"]) is int
                and record["evidence_line"] > 0
            )
        return terminal and tools > 0 and shape_ok
    except (ValueError, TypeError, AttributeError):
        return False


CODING_FIXTURE = PiFixture(
    name="coding",
    prompt=CODING,
    starter_outputs={"slugify.py": STARTER},
    worker_files={"verify.py": data("verify.py")},
    verifier_files={"verify.py": data("verify.py")},
    verify_entry="verify.py",
    candidate_output="slugify.py",
    candidate_name="candidate.py",
    verify_args=(),
    allowed_outputs=("slugify.py", "RESULT.json"),
    verifier_command=VERIFIER_COMMAND,
    expected_verifier_stdout=b"7 checks passed",
    completion=_coding_completion,
    report_contract=CODING_REPORT_CONTRACT,
)


def _augmented_key(raw_key, worker_files):
    """Fold in a hash of each acceptable evidence line; drop the line numbers
    themselves so the mounted key never carries more than hashes of the answer
    text.

    Several lines of the satisfying entry are accepted, because the measured
    capability is finding the right entry and pointing at real text in its own
    file that supports it -- not agreeing with the fixture author about which
    of that entry's claims is most decisive. The file is still matched exactly,
    so an index row that merely names the entry does not count as evidence.
    """
    key = {k: raw_key[k] for k in ("verdict", "entry", "evidence_path")}
    if raw_key["verdict"] == "satisfied":
        lines = worker_files[f"corpus/{raw_key['evidence_path']}"].splitlines()
        key["acceptable_line_sha256"] = sorted(
            hashlib.sha256(lines[n - 1].encode("utf-8")).hexdigest()
            for n in raw_key["evidence_lines"]
        )
    else:
        key["acceptable_line_sha256"] = None
    return key


def _manifest(corpus):
    """The corpus file listing, as a file inside the corpus.

    The worker has no shell and cannot read a directory, so without this it
    could not discover a single path -- which measures the sandbox, not the
    model. The listing is generated from the corpus itself so it cannot drift,
    and it names paths only: which entry satisfies the requirement is still
    found by reading them."""
    lines = "\n".join(f"- {name}" for name in sorted([*corpus, "MANIFEST.md"]))
    return (
        "# Corpus manifest\n\nEvery file in this corpus, relative to "
        f"/fixture/corpus/:\n\n{lines}\n"
    )


def INVESTIGATION_FIXTURE(variant):
    if variant not in ("findable", "absent"):
        raise ValueError("Unknown investigation variant")
    worker_corpus = data_tree(f"investigation/{variant}/corpus")
    worker_corpus["MANIFEST.md"] = _manifest(worker_corpus)
    worker_files = {f"corpus/{name}": text for name, text in worker_corpus.items()}
    raw_key = json.loads(data(f"investigation/{variant}/answer-key.json"))
    key = _augmented_key(raw_key, worker_files)
    verifier_files = {
        "verify_investigation.py": data("verify_investigation.py"),
        "answer-key.json": json.dumps(key, sort_keys=True),
        **{f"corpus/{name}": text for name, text in worker_corpus.items()},
    }
    entry_count = sum(
        1
        for name in worker_corpus
        if name.startswith("catalog/") or name.startswith("modules/")
    )
    corpus_sha256 = hashlib.sha256(
        json.dumps(worker_corpus, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return PiFixture(
        name="investigation",
        prompt=INVESTIGATION,
        starter_outputs={},
        worker_files=worker_files,
        verifier_files=verifier_files,
        verify_entry="verify_investigation.py",
        candidate_output="RESULT.json",
        candidate_name="RESULT.json",
        verify_args=("answer-key.json", "corpus"),
        allowed_outputs=("RESULT.json",),
        verifier_command=None,
        expected_verifier_stdout=b"9 checks passed",
        completion=_investigation_completion,
        tools="read,write,edit",
        variant=variant,
        corpus_sha256=corpus_sha256,
        entry_count=entry_count,
    )

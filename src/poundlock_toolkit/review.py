"""Blind-review packet export: anonymised candidate diffs, a rubric, a separate key.

"Checks pass" is not "mergeable" (see docs/METHODOLOGY.md). This module builds
the artefacts for an independent, blind rubric review: opaque labels assigned
in random order, one fixed rubric, and a label-to-identity key written to a
file the reviewer is never given. It never reads a repository, runs git, or
executes anything; candidates are pre-supplied diff text files, so this stays
a pure offline file transformation like `export`/`recommend`.
"""

import random
from pathlib import Path

DIFF_BYTES_LIMIT = 2_000_000
MIN_CANDIDATES = 2
MAX_CANDIDATES = 26  # one opaque label per letter, A-Z

RUBRIC = """# Blind review rubric

You are reviewing anonymised candidates independently. The label-to-identity key
is not part of this packet and is not needed to review; it is used only to score
once every verdict is recorded. Passing this toolkit's automated checks is
evidence, not a verdict: review the actual diff for correctness, scope,
duplication, dead code, test quality and any other merge blocker, exactly as you
would review a colleague's change with no author name attached.

For each candidate, record:

- A short list of concrete defects found, if any, each tagged BLOCKING or MINOR.
- One verdict: `merge as-is` (no blocking defects), `merge after small fix`
  (blocking, but narrow and clearly specified), or `rewrite` (blocking and not
  narrow, or the change does not do what it claims).

A model reviewing its own prior output is not an independent review: score every
candidate to the same rubric regardless of any guess about its origin, and do not
let one candidate's verdict anchor another's.
"""


def build_packet(candidates, rng=None):
    """Build the packet/rubric/key text from `candidates`.

    `candidates` is a list of `{"identity": str, "diff": str}` dicts (the real,
    private identity and the diff text already read from disk). Returns
    `{"packet": str, "rubric": str, "key": {label: identity}}`. `rng` is an
    injectable `random.Random` for deterministic tests; defaults to a fresh,
    unseeded one so real exports are not predictable from the packet alone.
    """
    if not (MIN_CANDIDATES <= len(candidates) <= MAX_CANDIDATES):
        raise ValueError(
            f"review-packet needs {MIN_CANDIDATES}-{MAX_CANDIDATES} candidates"
        )
    identities = [c["identity"] for c in candidates]
    if len(set(identities)) != len(identities):
        raise ValueError("Candidate identities must be unique")
    order = list(range(len(candidates)))
    (rng or random.Random()).shuffle(order)
    labels = [f"Candidate {chr(ord('A') + n)}" for n in range(len(candidates))]
    key = {}
    sections = []
    for label, index in zip(labels, order):
        candidate = candidates[index]
        key[label] = candidate["identity"]
        sections.append(f"## {label}\n\n```diff\n{candidate['diff']}\n```\n")
    packet = (
        "# Blind review packet\n\n"
        "Anonymised candidate diffs. See RUBRIC.md for the review method; the "
        "label-to-identity key is kept separately and is not part of this file.\n\n"
        + "\n".join(sections)
    )
    return {"packet": packet, "rubric": RUBRIC, "key": key}


def key_inside_packet(packet_dir, key_path):
    """Whether `key_path` is `packet_dir` itself or anywhere beneath it.

    The reviewer is handed the packet folder, so the key must never be written
    inside it. Both paths are resolved first, so `..` and symlinks cannot hide
    it. Neither path needs to exist yet.
    """
    packet, key = Path(packet_dir).resolve(), Path(key_path).resolve()
    return key == packet or packet in key.parents


def read_candidates(descriptor):
    """Load `{"candidates": [{"identity", "diff_path"}, ...]}` from a private
    descriptor, reading each diff file's bounded text content."""
    candidates = descriptor.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Descriptor needs a `candidates` array")
    result = []
    for entry in candidates:
        identity, diff_path = entry.get("identity"), entry.get("diff_path")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Each candidate needs a non-empty `identity`")
        if not isinstance(diff_path, str) or not diff_path:
            raise ValueError("Each candidate needs a `diff_path`")
        raw = Path(diff_path).read_text(errors="replace")
        if len(raw.encode()) > DIFF_BYTES_LIMIT:
            raise ValueError(f"{diff_path} exceeds the diff size limit")
        result.append({"identity": identity, "diff": raw})
    return result

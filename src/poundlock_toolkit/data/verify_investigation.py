"""Protected independent verifier for the investigation workload.

Run only inside a networkless sandbox with `python3 -I`, never inside the
worker container. Compares the worker's self-reported RESULT.json against a
pinned answer key using exact string equality for the entry id and evidence
path, and a hash of the corpus line the worker points at -- computed here from
an independent copy of the (non-secret) corpus, never trusted from the
worker's own quoted text -- so a fabricated quote cannot pass. Any of the
entry's own substantive lines is accepted; a line elsewhere in the corpus is
not. The answer key itself carries only pinned hashes, never the evidence
text, so reading this verifier's mount does not hand back the answer.
"""

import hashlib
import json
import sys
from pathlib import Path

REQUIRED_KEYS = frozenset({"verdict", "entry", "evidence_path", "evidence_line"})
VERDICTS = ("satisfied", "not_satisfied")


def verify(candidate_path, key_path, corpus_root):
    try:
        candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
        key = json.loads(Path(key_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not (isinstance(candidate, dict) and candidate.keys() == REQUIRED_KEYS):
        return False
    if candidate["verdict"] not in VERDICTS or candidate["verdict"] != key["verdict"]:
        return False
    if (
        candidate["entry"] != key["entry"]
        or candidate["evidence_path"] != key["evidence_path"]
    ):
        return False
    if key["verdict"] == "not_satisfied":
        return candidate["evidence_line"] is None
    if type(candidate["evidence_line"]) is not int or candidate["evidence_line"] <= 0:
        return False
    root = Path(corpus_root).resolve()
    try:
        target = (root / candidate["evidence_path"]).resolve()
    except (OSError, ValueError):
        return False
    if target != root and root not in target.parents:
        return False
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    if not (0 < candidate["evidence_line"] <= len(lines)):
        return False
    quoted = lines[candidate["evidence_line"] - 1]
    digest = hashlib.sha256(quoted.encode("utf-8")).hexdigest()
    return digest in key["acceptable_line_sha256"]


if __name__ == "__main__":
    try:
        passed = verify(sys.argv[1], sys.argv[2], sys.argv[3])
    except BaseException:
        passed = False
    print("9 checks passed" if passed else "verification failed")
    raise SystemExit(0 if passed else 1)

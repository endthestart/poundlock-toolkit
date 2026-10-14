"""Synthetic, frozen workload inputs; worker assertions are never verdicts."""

import json
from .core import digest

EXTRACTION = (
    "Extract this notice as JSON only, with exactly keys order_id (string), "
    "quantity (integer), and expedited (boolean). Notice: Order TK-104 contains "
    "7 replacement washers. Standard shipping; expedited delivery was not requested."
)
EXPECTED = {"order_id": "TK-104", "quantity": 7, "expedited": False}
TOOL_PROMPT = 'Call record_value exactly once with key "answer" and integer value 123. Do not use prose.'
TOOL = {
    "type": "function",
    "function": {
        "name": "record_value",
        "description": "Record a synthetic value without executing an action.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "value"],
            "properties": {"key": {"type": "string"}, "value": {"type": "integer"}},
        },
    },
}
CODING = (
    "Repair /work/slugify.py. Implement slugify(text): reject non-strings with TypeError; "
    "Unicode NFKD to ASCII ignoring unrepresentable characters; lowercase; replace each run "
    "of non-ASCII-alphanumeric characters with a hyphen; trim hyphens. Only change slugify.py "
    "and create RESULT.json. Use only imports re and unicodedata, one function, assignments, "
    "if/raise/return, isinstance and string/regex methods; no loops, comprehensions or dynamic evaluation. "
    "Shell access is restricted to the verifier; file tools may access only the task files and read the verifier. "
    "Run exactly: python3 -I /fixture/verify.py /work/slugify.py\n"
    'Then write RESULT.json with exactly {"status":"passed","verifier_exit_code":0} only '
    "if that verifier passed. Otherwise report failed. Do not alter protected fixtures."
)
INVESTIGATION = (
    "A read-only catalog is mounted at /fixture/corpus/. You have no shell and cannot list a "
    "directory: /fixture/corpus/MANIFEST.md lists every file in the catalog, so read it first. "
    "Determine whether any entry in the "
    "catalog satisfies capability requirement REQ-7: 'atomic append across multiple concurrent "
    "writers, with crash-safe recovery, and no reliance on any component outside the catalog.' "
    "Read as many catalog files as you need; an entry's own text may say plainly that it does "
    "not satisfy the requirement, and the entry that does satisfy it may not use the same words "
    "the requirement uses. A capability index or a pointer inside another entry may tell you "
    "where to look next; an index row is a pointer, not evidence. When you are done, write "
    "/work/RESULT.json and nothing else, containing "
    'exactly the keys {"verdict","entry","evidence_path","evidence_line"}: verdict is the '
    'string "satisfied" or "not_satisfied"; if satisfied, entry is that catalog entry\'s id, '
    "evidence_path is the path of the file describing that entry itself, relative to "
    '/fixture/corpus/ (in the form "section/file-name.md"), and evidence_line is the 1-based '
    "line number, in that same file, of a line that shows it satisfies REQ-7. "
    "If not_satisfied, set entry, evidence_path "
    "and evidence_line to null. Do not guess: only report a line you actually read at that path."
)


def unfence(text):
    """A single surrounding Markdown code fence, removed.

    Chat models commonly wrap JSON in a fence even when asked for JSON only.
    That is a formatting convention, not a protocol failure, and treating it as
    one blocked every later workload for models that are otherwise sound. The
    deviation is still recorded by `fenced_json`, never silently forgiven.
    """
    body = text.strip()
    if not body.startswith("```") or not body.endswith("```"):
        return body
    inner = body[3:-3]
    first, _, rest = inner.partition("\n")
    return (rest if not first.strip() or first.strip().isalnum() else inner).strip()


def fenced_json(text):
    """Whether this response needed unfencing to parse."""
    return unfence(text) != text.strip()


def extraction_pass(text):
    try:
        v = json.loads(unfence(text))
        return (
            type(v) is dict
            and v.keys() == EXPECTED.keys()
            and all(type(v[k]) is type(x) and v[k] == x for k, x in EXPECTED.items())
        )
    except (ValueError, TypeError):
        return False


def tool_pass(calls):
    try:
        if not isinstance(calls, list) or len(calls) != 1:
            return False
        f = calls[0]["function"]
        args = (
            json.loads(f["arguments"])
            if isinstance(f["arguments"], str)
            else f["arguments"]
        )
        return (
            f["name"] == "record_value"
            and args == {"key": "answer", "value": 123}
            and type(args["value"]) is int
        )
    except (ValueError, KeyError, TypeError):
        return False


def prompt_hash(workload, extra=None):
    base = {
        "extraction": EXTRACTION,
        "coding": CODING,
        "api": "Reply exactly OK",
        "warmup": "Reply exactly OK",
        "cache_probe": "prefix-cache probe",
        "tool": TOOL_PROMPT,
        "investigation": INVESTIGATION,
    }.get(workload, "readiness")
    return digest(base if extra is None else {"prompt": base, **extra})

"""Repeated direct schema qualification on either supported wire boundary."""

import copy
import signal
import time
import uuid
from typing import Any
from . import __version__
from .backends import (
    Backend,
    ollama_options,
    ollama_think,
    openai_sampling,
    reasoning_effort,
)
from .core import (
    Failure,
    atomic_json,
    bounded_signals,
    digest,
    identity,
    new_run_path,
    now,
    numeric,
    validate,
)
from .tool_schema import NAMESPACE, TOOL, contract, fixture, score, strict_json


def request(c, boundary):
    selected = dict(c, endpoint=c["tool_schema"]["endpoints"][boundary], api=boundary)
    s = c["settings"]
    payload = {
        "model": c["model"]["id"],
        "messages": fixture(c),
        "tools": [TOOL],
        "stream": False,
    }
    if boundary == "openai":
        payload.update(openai_sampling(s), tool_choice="required")
        if (effort := reasoning_effort(s)) is not None:
            payload["reasoning_effort"] = effort
        path = "/chat/completions"
    else:
        payload.update(options=ollama_options(s), think=ollama_think(s))
        path = "/api/chat"
    with Backend(selected).request(path, payload) as response:
        raw = response.read(c["limits"]["response_bytes"] + 1)
    if len(raw) > c["limits"]["response_bytes"]:
        raise Failure("protocol_failure", "response_limit")
    try:
        value = strict_json(raw)
        if boundary == "openai":
            choices = value["choices"]
            if len(choices) != 1:
                raise ValueError()
            message = choices[0]["message"]
            completed = choices[0].get("finish_reason") in ("stop", "tool_calls")
            # llama.cpp server-reported timing, when present; never a wall-rate
            # substitute. A collapse across a long qualification run is
            # evidence of a possible spill, never asserted as the cause.
            timing = value.get("timings") or {}
            predicted_per_second = numeric(timing.get("predicted_per_second"))
        else:
            message = value["message"]
            completed = value.get("done") is True and value.get("done_reason") in (
                "stop",
                None,
            )
            eval_count, eval_duration = (
                numeric(value.get("eval_count")),
                numeric(value.get("eval_duration")),
            )
            # Not Backend.ollama's rate: a positive duration too small to be a
            # nonzero number of seconds is null there and raises here. Kept
            # apart until that difference is decided (dead-code review, M4).
            predicted_per_second = (
                eval_count / (eval_duration / 1e9)
                if eval_count is not None and eval_duration
                else None
            )
        calls = message.get("tool_calls", [])
        if type(calls) is not list:
            raise ValueError()
        return calls, completed, predicted_per_second
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ):
        raise Failure("protocol_failure", "malformed_response") from None


def trial_score(calls, completed):
    empty: dict[str, Any] = {
        "argument_representation": "missing",
        "outer_parse_success": None,
        "schema_valid": None,
        "errors": [],
        "errors_truncated": False,
    }
    result = dict(
        empty,
        call_count=len(calls),
        correct_tool=False,
        completion_pass=completed,
        valid_call=False,
    )
    if len(calls) != 1:
        return result, "missing_call" if not calls else "multiple_calls"
    try:
        fn = calls[0]["function"]
        result["correct_tool"] = fn["name"] == "propose_plan"
        if "arguments" not in fn:
            return result, "missing_arguments"
        result.update(score(fn["arguments"]))
    except (TypeError, KeyError):
        return result, "malformed_call"
    result["valid_call"] = bool(
        completed and result["correct_tool"] and result["schema_valid"]
    )
    code = (
        "ok"
        if result["valid_call"]
        else "wrong_tool"
        if not result["correct_tool"]
        else "missing_completion"
        if not completed
        else "invalid_arguments"
    )
    return result, code


def evaluate(c, output, *, progress=None, requester=None):
    from .planning import plan

    if plan(c)["blockers"]:
        raise ValueError("Plan is blocked")
    if not hasattr(signal, "setitimer"):
        raise ValueError("Bounded execution requires Linux or macOS")
    output = new_run_path(output)
    requester = requester or request
    spec = contract(c)
    doc = {
        "schema": NAMESPACE,
        "schema_version": 2,
        "toolkit_version": __version__,
        "run_id": str(uuid.uuid4()),
        "created_at": now(),
        "config_sha256": digest(c),
        "identity_sha256": digest(identity(c)),
        "contract": spec,
        # The settings this boundary was measured under. Recorded so a stored
        # measurement can be compared against how the model is configured
        # later: a tool-call result at one context window says nothing about
        # another. Absent from documents written before this field existed.
        "settings": {
            k: c["settings"][k]
            for k in ("context_tokens", "max_output_tokens", "reasoning")
        },
        "limits": {
            k: c["limits"][k]
            for k in ("total_seconds", "request_seconds", "response_bytes")
        },
        "attempts": [],
        "warmup": [
            {
                "boundary": boundary,
                "started_at": None,
                "completed_at": None,
                "outcome": "skipped",
                "code": "not_run",
                "latency_seconds": None,
                "predicted_per_second": None,
            }
            for boundary in spec["boundaries"]
        ]
        if spec["warmup_enabled"]
        else [],
    }
    # Materialize all planned rows before any request, preserving crash denominators.
    for rep in range(1, spec["trials_per_boundary"] + 1):
        for boundary in spec["boundaries"]:
            doc["attempts"].append(
                {
                    "id": str(uuid.uuid4()),
                    "boundary": boundary,
                    "repetition": rep,
                    "started_at": None,
                    "completed_at": None,
                    "outcome": "skipped",
                    "code": "not_run",
                    "latency_seconds": None,
                    "call_count": None,
                    "correct_tool": None,
                    "completion_pass": None,
                    "valid_call": False,
                    "argument_representation": "missing",
                    "outer_parse_success": None,
                    "schema_valid": None,
                    "errors": [],
                    "errors_truncated": False,
                    "interventions": [],
                    "predicted_per_second": None,
                }
            )
    persist(output, doc)
    deadline = time.monotonic() + c["limits"]["total_seconds"]
    cancelled = False

    def run(row, scored):
        """One bounded request for `row`. A warm-up row (`scored` false) records
        the verdict only, never the argument diagnostics."""
        nonlocal cancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled:
            row["code"] = "not_run" if cancelled else "budget_exhausted"
            return
        row.update(started_at=now(), outcome="running", code="pending")
        persist(output, doc)
        started = time.monotonic()
        try:
            signal.setitimer(
                signal.ITIMER_REAL, min(remaining, c["limits"]["request_seconds"])
            )
            if progress:
                if scored:
                    progress("tool_schema", row["boundary"], row["repetition"])
                else:
                    progress("tool_schema_warmup", row["boundary"], 0)
            calls, completed, predicted_per_second = requester(c, row["boundary"])
            result, code = trial_score(calls, completed)
            row.update(
                result if scored else {},
                code=code,
                outcome="pass" if result["valid_call"] else "schema_failure",
                predicted_per_second=predicted_per_second,
            )
        except Failure as failure:
            row.update(outcome=failure.outcome, code=failure.code)
        except KeyboardInterrupt:
            cancelled = True
            row.update(outcome="cancelled", code="cancelled")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            row.update(outcome="protocol_failure", code="malformed_response")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            row.update(latency_seconds=time.monotonic() - started, completed_at=now())
            persist(output, doc)

    restore_signals = bounded_signals()
    try:
        # Unscored: run once per boundary, before any scored trial, so
        # first-load costs (e.g. Vulkan shader compilation) never land inside
        # a scored attempt. Never affects `summary` or promotion.
        for row in doc["warmup"]:
            run(row, scored=False)
        for row in doc["attempts"]:
            run(row, scored=True)
    finally:
        restore_signals()
        persist(output, doc)
    return copy.deepcopy(doc)


def summaries(doc):
    """Derive counts from trial rows, avoiding stale summaries after crash recovery.

    `valid`/`invalid`/`timed_out`/`not_run` split what a single `passed` count
    used to flatten: a model whose every trial timed out previously reported
    as indistinguishable from one that answered wrong every time ("0/5"),
    when the two call for different next steps.
    """
    result = {}
    for boundary in doc["contract"]["boundaries"]:
        rows = [r for r in doc["attempts"] if r["boundary"] == boundary]
        attempted = [r for r in rows if r["started_at"] is not None]
        passed = sum(r["valid_call"] for r in attempted)
        timed_out = sum(r["outcome"] == "timeout" for r in attempted)
        result[boundary] = {
            "planned": len(rows),
            "attempted": len(attempted),
            "passed": passed,
            "valid_call_rate": passed / len(attempted) if attempted else None,
            "outer_parse_successes": sum(
                r["outer_parse_success"] is True for r in attempted
            ),
            "schema_valid_calls": sum(r["schema_valid"] is True for r in attempted),
            "valid": passed,
            "timed_out": timed_out,
            "invalid": len(attempted) - passed - timed_out,
            "not_run": len(rows) - len(attempted),
        }
    return result


def report(documents):
    """The tool-call part of reporting.report, which validates each document."""
    out = [
        "# Tool-call schema adherence",
        "",
        "Strict fixture-specific evidence; nested JSON strings are never repaired or accepted. This is not task-quality or production-admission evidence.",
        "",
    ]
    for doc in documents:
        out += [
            f"Run {doc['run_id']}; configuration `{doc['identity_sha256']}`.",
            f"Mode: {doc['contract']['mode']}; conversation: {doc['contract']['conversation_source']}; fixture `{doc['contract']['fixture_sha256']}`.",
            "",
        ]
        if doc.get("warmup"):
            out.append(
                "Warm-up (unscored, excluded from the counts below): "
                + "; ".join(
                    f"{w['boundary']}: {w['outcome']} ({w['code']})"
                    for w in doc["warmup"]
                )
            )
            out.append("")
        for boundary, counts in summaries(doc).items():
            rate = (
                "unknown"
                if counts["valid_call_rate"] is None
                else f"{counts['valid_call_rate']:.1%}"
            )
            label = "OpenAI-compatible" if boundary == "openai" else "native Ollama"
            out += [
                f"{label}: {counts['valid']}/{counts['attempted']} valid ({rate}); "
                f"{counts['timed_out']} timed out; {counts['invalid']} invalid; "
                f"{counts['not_run']} not run; {counts['planned']} planned. "
                f"Outer arguments parsed: {counts['outer_parse_successes']}; schema-valid: {counts['schema_valid_calls']}."
            ]
        out += [
            "",
            "| API | Trial | Outcome | Latency seconds | Field/type diagnostics |",
            "|---|---:|---|---:|---|",
        ]
        for row in doc["attempts"]:
            errors = (
                "; ".join(
                    f"{e['path']}: expected {e['expected']}, got {e['actual']} ({e['keyword']}; nested JSON {e['nested_json'] or 'not applicable'})"
                    for e in row["errors"]
                )
                or "none"
            )
            out.append(
                f"| {row['boundary']} | {row['repetition']} | {row['outcome']} ({row['code']}) | {row['latency_seconds'] if row['latency_seconds'] is not None else 'unknown'} | {errors} |"
            )
        out += [
            "",
            "Compare only matching model/settings/fixture/conversation identities. Missing trials and transport failures remain visible. Latency includes the full request and local parsing; prompt/decode/cold-cache timing is unmeasured. For smoke evidence, next run a bounded qualification with a representative longer conversation. Live model validity and semantic plan quality require separate acceptance.",
            "",
        ]
    return "\n".join(out)


def persist(output, doc):
    doc["summary"] = summaries(doc)
    atomic_json(output, validate(doc))

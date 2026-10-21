"""Serial staged gates and crash-recoverable attempt evidence."""

from __future__ import annotations
import copy
import signal
import time
import uuid
from . import __version__
from .backends import Backend, chat_messages, empty_metrics
from .cache_probe import (
    REQUESTS,
    derive,
    measured_setting,
    probe_prompts,
    probe_request,
)
from .core import (
    Failure,
    atomic_json,
    bounded_signals,
    digest,
    identity,
    new_run_path,
    now,
    validate,
)
from .fixtures import CODING_FIXTURE, INVESTIGATION_FIXTURE
from .memory_probe import INTERVAL_SECONDS, open_sampler
from .pi import PiRunner, active_compaction_gate
from .planning import plan
from .workloads import (
    EXTRACTION,
    TOOL_PROMPT,
    extraction_pass,
    fenced_json,
    prompt_hash,
    tool_pass,
)

# Workloads that run inside the Pi sandbox and therefore need the same
# tool-calling and streaming gates before their task attempts are promoted.
PI_WORKLOADS = ("coding", "investigation")


def identity_complete(c, measured_cache=None):
    """Whether every identity field is known.

    The prefix cache counts as known when the operator declared it, or when the
    probe measured it (`measured_cache` is `enabled` or `disabled`). An
    inconclusive probe leaves a declared `unknown` unknown.
    """
    s = c["settings"]
    complete = all(
        [
            c["model"]["revision"],
            c["model"]["quantization"],
            c["model"]["format"] != "unknown",
            c["backend"]["version"],
            s["template_sha256"],
            s["prefix_cache"] != "unknown" or measured_cache in ("enabled", "disabled"),
            s["kv_quantization"],
        ]
    )
    if c["harness"]["name"] == "pi":
        complete = complete and all(c["harness"][x] for x in ("version", "image"))
    serving = serving_identity(c)
    # A backend build/commit can be shared by several serving-application releases
    # (e.g. two wrapper releases on one llama.cpp build), so both must be recorded.
    complete = complete and serving["application_status"] != "unknown"
    if serving["application_status"] == "recorded":
        complete = complete and serving["application_version"] is not None
    return bool(complete)


def new_document(c, synthetic=False):
    s = c["settings"]
    serving = serving_identity(c)
    document = {
        "schema": "urn:poundlock-toolkit:qualification:1",
        "schema_version": 2,
        "toolkit_version": __version__,
        "run_id": str(uuid.uuid4()),
        "created_at": now(),
        "synthetic": synthetic,
        "config_sha256": digest(c),
        "identity_sha256": digest(identity(c)),
        "backend": c["backend"]["name"],
        "api": c["api"],
        "harness": c["harness"]["name"],
        "identity_complete": identity_complete(c),
        "serving": serving,
        "data_destination": c["data_destination"],
        "inventory": copy.deepcopy(c["inventory"]),
        "settings": copy.deepcopy(s),
        "requirements": copy.deepcopy(c["requirements"]),
        "limits": copy.deepcopy(c["limits"]),
        "attempts": [],
        "legacy_sha256": None,
    }
    if "investigation" in c["workloads"]:
        fx = INVESTIGATION_FIXTURE(c["investigation"]["variant"])
        document["investigation"] = {
            "variant": fx.variant,
            "corpus_sha256": fx.corpus_sha256,
            "entry_count": fx.entry_count,
        }
    if "coding" in c["workloads"] and (gate := active_compaction_gate(c, "coding")):
        # What the coding attempts ran under (`settings.context_tokens` is not
        # it), and what recommendation() needs to know: a gate run that never
        # compacted did not exercise the gate.
        document["compaction_gate"] = {
            k: gate[k]
            for k in ("context_tokens", "reserve_tokens", "keep_recent_tokens")
        }
    return document


def serving_identity(c):
    """Serving application release, recorded separately from backend build/commit.

    `backend.application` absent means unrecorded (unknown); null means the backend
    serves directly with no wrapping application.
    """
    backend = c["backend"]
    application = backend.get("application", "unrecorded")
    return {
        "backend_version": backend["version"],
        "backend_commit": backend.get("commit"),
        "application_status": "unknown"
        if application == "unrecorded"
        else "none"
        if application is None
        else "recorded",
        "application": application["name"] if isinstance(application, dict) else None,
        "application_version": application["version"]
        if isinstance(application, dict)
        else None,
    }


def attempt(stage, workload, rep=0, prompt_extra=None):
    return {
        "id": str(uuid.uuid4()),
        "stage": stage,
        "workload": workload,
        "repetition": rep,
        "started_at": now(),
        "completed_at": None,
        "outcome": "running",
        "code": "pending",
        "metrics": empty_metrics(),
        "cache_state": "unknown",
        "prompt_sha256": prompt_hash(workload, prompt_extra),
        "response_sha256": None,
        "independent_pass": None,
        "completion_pass": None,
        "cleanup": "not_needed",
        "interventions": [],
    }


def evaluate(
    c,
    output,
    *,
    backend=None,
    pi_factory=PiRunner,
    progress=None,
    synthetic=False,
    readiness_only=False,
    memory_reader=None,
    memory_interval=INTERVAL_SECONDS,
):
    if "tool_schema" in c["workloads"]:
        if synthetic or backend is not None or readiness_only:
            raise ValueError(
                "Schema scenarios use direct evaluation; demo/readiness backends are unsupported"
            )
        from .schema_evaluation import evaluate as schema_evaluate

        return schema_evaluate(c, output, progress=progress)
    if plan(c, readiness_only)["blockers"]:
        raise ValueError("Plan is blocked; resolve its stated prerequisites")
    if not hasattr(signal, "setitimer"):
        raise ValueError("Bounded execution currently requires Linux or macOS")
    output = new_run_path(output)
    backend = backend or Backend(c)
    doc = new_document(c, synthetic=synthetic)
    memory = None
    if c.get("memory_probe") and not (readiness_only or synthetic):
        # Observes an already running local process; a synthetic run has no real
        # server to observe, and readiness-only sends no inference to measure.
        memory, gap = open_sampler(c["memory_probe"], memory_reader, memory_interval)
        if memory is None:
            doc["memory_probe"] = gap
    atomic_json(output, doc)
    deadline = time.monotonic() + c["limits"]["total_seconds"]
    cancelled = False
    # What the server returned to each probe request, by repetition: the agent-turn
    # request appends after it. Held only for the run; never written to the document.
    probe_replies = {}

    def execute(stage, workload, rep=0, blocked=False, prompt=None, history=()):
        nonlocal cancelled
        prompt_extra = None
        if workload == "investigation":
            fx = INVESTIGATION_FIXTURE(c["investigation"]["variant"])
            prompt_extra = {"variant": fx.variant, "corpus_sha256": fx.corpus_sha256}
        elif workload == "cache_probe":
            prompt_extra = {"text_sha256": digest(chat_messages(prompt, history))}
        row = attempt(stage, workload, rep, prompt_extra)
        doc["attempts"].append(row)
        atomic_json(output, doc)
        if progress:
            progress(stage, workload, rep)
        pi = None
        mark = None
        started = time.monotonic()
        try:
            if blocked or cancelled:
                row.update(outcome="skipped", code="not_run")
                return False
            remaining = deadline - started
            if remaining <= 0:
                row.update(outcome="skipped", code="budget_exhausted")
                return False
            if memory is not None:
                mark = memory.mark()
            signal.setitimer(
                signal.ITIMER_REAL, min(remaining, c["limits"]["request_seconds"])
            )
            if stage == "readiness":
                result = backend.readiness()
            elif stage == "warmup":
                # Unscored: triggers first-load costs (e.g. Vulkan shader
                # compilation) so they never land inside a scored trial.
                # independent_pass/completion_pass stay unset (null), the
                # same shape as a readiness attempt, so it never affects
                # promotion or the pass/fail gates below.
                result = backend.complete("Reply exactly OK", tool=False, stream=False)
            elif stage == "cache_probe":
                # Unscored, like the warm-up: what is measured is how the server
                # processes the prompt, never the answer.
                result = backend.probe(prompt, history)
                probe_replies[rep] = result["text"]
            elif workload in PI_WORKLOADS:
                pi = pi_factory(c)
                pi.journal = output.with_name(
                    output.stem + "-" + row["id"] + ".owned.json"
                )
                fixture = (
                    CODING_FIXTURE
                    if workload == "coding"
                    else INVESTIGATION_FIXTURE(c["investigation"]["variant"])
                )
                result = pi.run(fixture)
            else:
                prompt = (
                    "Reply exactly OK"
                    if workload == "api"
                    else TOOL_PROMPT
                    if workload == "tool"
                    else EXTRACTION
                )
                result = backend.complete(
                    prompt, tool=workload == "tool", stream=stage == "performance"
                )
                passed = (
                    result["text"].strip() == "OK"
                    if workload == "api"
                    else tool_pass(result["calls"])
                    if workload == "tool"
                    else extraction_pass(result["text"])
                )
                result.update(independent_pass=passed, completion_pass=True)
                if workload == "extraction" and fenced_json(result["text"]):
                    # Passed, but wrapped in a code fence: recorded so the
                    # evidence shows the deviation.
                    row["interventions"] = [
                        *row.get("interventions", []),
                        "fenced_json",
                    ]
            for key in (
                "metrics",
                "response_sha256",
                "independent_pass",
                "completion_pass",
            ):
                if key in result:
                    row[key] = result[key]
            if row["independent_pass"] is False:
                row.update(
                    outcome="protocol_failure"
                    if stage in ("api", "tool")
                    else "task_quality_failure",
                    code="wrong_output",
                )
            elif row["completion_pass"] is False:
                row.update(outcome="completion_failure", code="missing_completion")
            else:
                row.update(outcome="pass", code="ok")
        except Failure as failure:
            row.update(outcome=failure.outcome, code=failure.code)
        except KeyboardInterrupt:
            cancelled = True
            row.update(
                outcome="cancelled", code="cancelled", interventions=["operator_cancel"]
            )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            row.update(outcome="infrastructure_failure", code="unavailable")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if pi:
                row["cleanup"] = pi.cleanup
                if pi.cleanup == "failed":
                    row.update(outcome="infrastructure_failure", code="cleanup_failed")
            row["completed_at"] = now()
            # Attempt wall includes failure/cleanup. Missing stage timing stays null.
            if row["outcome"] != "skipped" and row["metrics"]["wall_seconds"] is None:
                row["metrics"]["wall_seconds"] = time.monotonic() - started
            if memory is not None and mark is not None:
                # Highest resident set sampled during this attempt; null if no
                # sample could be read.
                row["metrics"]["peak_process_rss_bytes"] = memory.peak_since(mark)
            validate(doc)
            atomic_json(output, doc)
        return row["outcome"] == "pass"

    def probe_prefix_cache(ready):
        """Four unscored requests, then the verdict and what it does to identity."""
        base, edited = probe_prompts(
            c["settings"]["context_tokens"], uuid.uuid4().hex[:12]
        )
        rows, proceed = [], ready
        for rep, kind in enumerate(REQUESTS, start=1):
            reply = probe_replies.get(rep - 1)
            prompt, history = probe_request(kind, base, edited, reply or "")
            # A failed request ends the probe: the rest would only spend budget. So
            # does a reply that says nothing: an agent turn has none to append after.
            blocked = not proceed or (kind == "agent_turn" and not reply)
            proceed = execute(
                "cache_probe", "cache_probe", rep, blocked, prompt, history
            )
            rows.append(doc["attempts"][-1])
        warmed = any(
            a["stage"] == "warmup" and a["outcome"] == "pass" for a in doc["attempts"]
        )
        doc["prefix_cache_probe"] = derive(rows, warmed)
        doc["identity_complete"] = identity_complete(
            c, measured_setting(doc["prefix_cache_probe"])
        )
        validate(doc)
        atomic_json(output, doc)

    restore_signals = bounded_signals()
    try:
        if memory is not None:
            memory.start()
        ready = execute("readiness", "readiness")
        if readiness_only:
            return doc
        if c["limits"].get("warmup_enabled", True):
            # Best-effort and unscored: never gates the "api" probe below, so
            # a flaky warm-up cannot turn into a spurious infrastructure block.
            execute("warmup", "warmup", blocked=not ready)
        if c["limits"].get("prefix_cache_probe_enabled", False):
            # After the warm-up, so a first request's one-off costs are not
            # mistaken for a cache miss (or a hit on the repeat).
            probe_prefix_cache(ready)
        api = execute("api", "api", blocked=not ready)
        tool = (
            execute("tool", "tool", blocked=not api)
            if any(w in c["workloads"] for w in PI_WORKLOADS)
            else True
        )
        # Protocol streaming performance has its own evidence. A failed streaming
        # gate does not erase nonstream extraction evidence, but blocks Pi promotion.
        perf = execute("performance", "extraction", blocked=not api)
        for workload in c["workloads"]:
            promoted = api and (tool and perf if workload in PI_WORKLOADS else True)
            for repetition in range(1, c["limits"]["repetitions"] + 1):
                passed = execute("task", workload, repetition, blocked=not promoted)
                if repetition == 1:
                    promoted = passed
    finally:
        restore_signals()
        if memory is not None:
            memory.stop()
            doc["memory_probe"] = memory.summary()
            validate(doc)
            atomic_json(output, doc)
    return doc


def recover(doc):
    validate(doc)
    doc = copy.deepcopy(doc)
    for row in doc["attempts"]:
        if row["outcome"] == "running":
            row.update(
                outcome="interrupted",
                code="interrupted",
                completed_at=now(),
                interventions=["recovered_after_interruption"],
            )
    for row in doc.get("warmup", []):
        if row["outcome"] == "running":
            row.update(outcome="interrupted", code="interrupted", completed_at=now())
    if doc["schema"] == "urn:poundlock-toolkit:tool-schema:1":
        from .schema_evaluation import summaries

        doc["summary"] = summaries(doc)
    return validate(doc)

"""Deterministic per-workload explanations, with all attempted runs visible."""

import copy
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from .cache_probe import EDIT_VERDICT_TEXT, describe, finding, reason_text
from .core import digest, validate
from .planning import PI_WORKLOADS

# Evidence older than this is reported stale: the target, runtime or model may have
# changed since it was measured. Re-run the bounded plan to refresh it.
FRESHNESS = timedelta(days=30)

CHECK_ORDER = (
    "measured",
    "freshness",
    "identity",
    "serving_application",
    "requirements",
    "capacity",
    "gates",
    "latency",
    "throughput",
    "compaction",
    "repetitions",
)


def export(doc):
    # The internal result is already an allowlist. Reject any added fields rather
    # than risk a newly introduced free-form field silently crossing the boundary.
    validate(doc)
    return copy.deepcopy(doc)


def serving_status(doc):
    serving = doc.get("serving")
    if serving is None or serving["application_status"] == "unknown":
        return "unknown"
    if (
        serving["application_status"] == "recorded"
        and serving["application_version"] is None
    ):
        return "unknown"
    return "pass"


def serving_text(doc):
    serving = doc.get("serving")
    if serving is None:
        return "Serving application release: not recorded by this evidence version."
    build = serving["backend_version"] or "unknown"
    commit = serving["backend_commit"] or "unknown"
    if serving["application_status"] == "recorded":
        app = f"{serving['application']} {serving['application_version'] or '(version unknown)'}"
    elif serving["application_status"] == "none":
        app = "none (backend serves directly)"
    else:
        app = "not recorded"
    return (
        f"Serving application release: {app}. Backend build: {build}; commit: {commit}."
    )


def prefix_cache_text(doc):
    """Declared against measured prefix cache, in one or two sentences.

    The measured value is the one reported; the declared one is shown beside it,
    and a disagreement is said plainly. Without a probe, or with an inconclusive
    one, the declared setting is all there is, and the text says so.
    """
    f = finding(doc)
    declared = f"declared {f['declared']}"
    if f["probe"] is None:
        return (
            f"Prefix cache: not measured ({declared}; the probe is off, so this is the "
            "operator's statement, not an observation)."
        )
    probe = f["probe"]
    if f["measured"] is None:
        return (
            f"Prefix cache: probe inconclusive ({reason_text(probe)}); "
            + (
                f"{declared}, which stands."
                if f["declared"] != "unknown"
                else "declared unknown, so it stays unknown."
            )
            + edit_inside_text(probe, settled=False)
        )
    # A block from 0.2.1 rested on the repeat and an extension, and reads as such.
    later = (
        f"agent turn: {describe(probe['agent_turn'])}"
        if "agent_turn" in probe
        else f"extension: {describe(probe['extension'])}"
    )
    evidence = f"repeat: {describe(probe['repeat'])}; {later}"
    agreement = (
        f"MISMATCH with {declared}; the measured value is the one reported and the "
        "declaration should be corrected"
        if f["mismatch"]
        else f"{declared}, consistent"
        if f["declared"] != "unknown"
        else f"{declared}; the measurement stands in for it"
    )
    return (
        f"Prefix cache: observed {probe['observed']} ({evidence}); {agreement}."
        + edit_inside_text(probe, settled=True)
    )


def edit_inside_text(probe, settled):
    """What an edit inside the last message did, apart from the verdict.

    Empty for a block that predates the comparison, and, while the verdict itself
    is unsettled, for an edit that measured nothing.
    """
    verdict = probe.get("edit_inside_verdict")
    edit = probe.get("edit_inside")
    if verdict is None or not (
        settled or edit["cached_fraction"] is not None or edit["speedup"] is not None
    ):
        return ""
    return (
        f" Edit inside the last message: {EDIT_VERDICT_TEXT[verdict]} "
        f"({describe(edit)})."
    )


def size(value):
    if value < 2**30:
        return f"{value / 2**20:.0f} MiB"
    return f"{value / 2**30:.2f} GiB"


def memory_text(doc):
    """What the memory probe measured, and what that does not show."""
    m = doc.get("memory_probe")
    if m is None:
        return (
            "Memory: not measured (no memory_probe configured); the model's peak "
            "memory is unknown."
        )
    if m["status"] != "sampled":
        return f"Memory: probe unavailable ({m['reason']}); the model's peak memory is unknown."
    who = f" ({m['process_name']})" if m["process_name"] else ""
    lines = [
        f"Memory: {m['samples']} samples of the server process{who} every "
        f"{m['interval_seconds']}s and at each attempt boundary. Highest sampled "
        f"process RSS {size(m['peak_process_rss_bytes'])} (first sample "
        f"{size(m['first_process_rss_bytes'])}), a lower bound on the true peak and "
        "host memory only."
    ]
    if m["process_exited"]:
        lines.append(
            "The process ended, or its PID was reused, before the run finished; "
            "later samples are missing."
        )
    for pool in m["gpu_pools"]:
        total = f" of {size(pool['total_bytes'])}" if pool["total_bytes"] else ""
        lines.append(
            f"GPU card{pool['drm_card']} VRAM in use, device-wide (every process on "
            f"the card, not only the server): first {size(pool['first_used_bytes'])}, "
            f"peak {size(pool['peak_used_bytes'])}{total}."
        )
    if not m["gpu_pools"]:
        lines.append(
            "No GPU memory counter was read (unknown, not zero); GPU and "
            "Apple-silicon unified memory are not established by process RSS."
        )
    else:
        lines.append("GPU pools are reported separately and never summed.")
    return " ".join(lines)


def recommendation(doc, workload, now=None):
    validate(doc)
    return _recommendation(doc, workload, now)


def _recommendation(doc, workload, now=None):
    """`recommendation` for a document that has already been validated."""
    now = now or datetime.now(timezone.utc)
    checks = {}
    rows = [
        a for a in doc["attempts"] if a["stage"] == "task" and a["workload"] == workload
    ]
    attempted = [a for a in rows if a["outcome"] != "skipped"]
    passed = [
        a
        for a in attempted
        if a["outcome"] == "pass"
        and a["independent_pass"] is True
        and a["completion_pass"] is True
        and a["cleanup"] != "failed"
    ]
    reasons = []
    # Visible in the report but never a reason: they cannot change the level.
    notes = []
    created = datetime.fromisoformat(doc["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    checks["freshness"] = "stale" if now - created > FRESHNESS else "pass"
    if checks["freshness"] == "stale":
        reasons.append(
            f"Evidence is stale: measured more than {FRESHNESS.days} days ago."
        )
    checks["measured"] = "fail" if doc["synthetic"] or doc["legacy_sha256"] else "pass"
    if doc["synthetic"]:
        reasons.append(
            "Synthetic demonstration; not measured model or hardware qualification."
        )
    if doc["legacy_sha256"]:
        reasons.append(
            "Legacy envelope is provenance only; historical measurements were not reinterpreted."
        )
    checks["identity"] = "pass" if doc["identity_complete"] else "unknown"
    if not doc["identity_complete"]:
        reasons.append("Exact model/backend/template/cache identity is incomplete.")
    cache = finding(doc)
    if cache["mismatch"]:
        # The record's declared identity contradicts what was observed, so it does
        # not describe the configuration that was measured.
        checks["identity"] = "fail"
        reasons.append(
            f"Declared prefix cache ({cache['declared']}) contradicts the measured "
            f"result ({cache['observed']}: {reason_text(cache['probe'])})."
        )
    checks["serving_application"] = serving_status(doc)
    if checks["serving_application"] != "pass":
        reasons.append(
            "Serving application release is not recorded; a backend build/commit alone cannot distinguish application releases."
        )
    checks["requirements"] = "pass"
    if doc["data_destination"] not in doc["requirements"]["allowed_destinations"]:
        reasons.append("Required data destination is not established.")
        checks["requirements"] = "fail"
    if doc["settings"]["context_tokens"] < doc["requirements"]["min_context_tokens"]:
        reasons.append("Insufficient declared context.")
        checks["requirements"] = "fail"
    available = doc["inventory"]["memory"]["system_available_bytes"]
    required = doc["requirements"]["min_available_memory_bytes"]
    checks["capacity"] = (
        "pass"
        if required is None
        else "unknown"
        if available is None
        else "pass"
        if available >= required
        else "fail"
    )
    if required is not None and (available is None or available < required):
        reasons.append("Required memory headroom is unknown or insufficient.")
    times = [
        r["metrics"]["wall_seconds"]
        for r in passed
        if r["metrics"]["wall_seconds"] is not None
    ]
    maximum = doc["requirements"]["max_task_seconds"]
    checks["latency"] = "pass"
    if maximum is not None and (
        len(times) != len(passed) or not times or max(times) > maximum
    ):
        reasons.append("Required task latency is unknown or exceeded.")
        checks["latency"] = "fail" if times and max(times) > maximum else "unknown"
    # Absent (or evidence recorded before this requirement existed) means no
    # throughput floor was configured, not that one was met.
    min_decode = doc["requirements"].get("min_decode_tokens_per_second")
    decode_rates = [
        r["metrics"]["decode_tokens_per_second"]
        for r in passed
        if r["metrics"]["decode_tokens_per_second"] is not None
    ]
    checks["throughput"] = "pass"
    if min_decode is not None:
        if not decode_rates and workload in PI_WORKLOADS:
            # Pi's event stream carries token counts but no server timing, and a
            # rate is never derived from wall-clock time, so a Pi run records no
            # decode speed at all. The floor has nothing to be measured against
            # here: leave the verdict alone, and say it was not applied.
            notes.append(
                f"The decode-speed floor ({min_decode} tok/s) was not applied to {workload}: "
                "Pi runs record no server-reported decode speed."
            )
        elif not decode_rates:
            checks["throughput"] = "unknown"
            reasons.append(
                "Required decode speed is unknown: no passed attempt reported it."
            )
        elif min(decode_rates) < min_decode:
            checks["throughput"] = "fail"
            reasons.append(
                f"Warm decode speed ({min(decode_rates):.1f} tok/s) fell below the required "
                f"minimum ({min_decode} tok/s); a possible explanation is a VRAM spill, not a "
                "confirmed cause."
            )
    # A configured compaction gate only means something if a compaction
    # completed during the run it qualifies. A passed attempt that recorded none
    # (or did not record the count) says nothing about whether the report
    # contract survives one, so it must not count toward qualification. No
    # recorded gate means none was on, which is not a failure.
    checks["compaction"] = "pass"
    if workload == "coding" and doc.get("compaction_gate"):
        uncompacted = [r for r in passed if not r["metrics"].get("compactions")]
        if uncompacted:
            checks["compaction"] = "unknown"
            reasons.append(
                f"The compaction gate did not trigger a compaction in {len(uncompacted)} of "
                f"{len(passed)} passed attempt(s), so it was not exercised; a pass without one "
                "is not evidence that the report contract survives compaction."
            )
    required_gates = set(
        ("readiness", "api", "tool", "performance")
        if workload in PI_WORKLOADS
        else ("readiness", "api")
    )
    gates = [a for a in doc["attempts"] if a["stage"] in required_gates]
    failed_gate = any(a["outcome"] not in ("pass", "skipped", "running") for a in gates)
    if {a["stage"] for a in gates} != required_gates or any(
        a["outcome"] != "pass" for a in gates
    ):
        reasons.append("A required readiness or compatibility gate did not pass.")
        checks["gates"] = "fail" if failed_gate else "unknown"
    else:
        checks["gates"] = "pass"
    checks["repetitions"] = (
        "fail"
        if len(passed) != len(attempted)
        else "unknown"
        if len(passed) < 3
        else "pass"
    )
    if len(passed) != len(attempted) or not passed:
        reasons.append(
            "At least one attempt failed, was interrupted, or has no verified pass."
        )
    if len(passed) < 3:
        reasons.append(
            "Fewer than three verified repetitions; repeated qualification is not established."
        )
    level = "insufficient evidence" if reasons else "repeated task qualification"
    if (
        not doc["synthetic"]
        and len(passed) == 1
        and len(attempted) == 1
        and len(reasons) == 1
        and reasons[0].startswith("Fewer")
    ):
        level = "observed smoke pass"
    ordered = {name: checks[name] for name in CHECK_ORDER}
    pending = [name for name, status in ordered.items() if status != "pass"]
    return {
        "workload": workload,
        "level": level,
        "checks": ordered,
        "next_required_check": pending[0] if pending else None,
        "passed": len(passed),
        "attempted": len(attempted),
        "planned": len(rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "median_task_seconds": statistics.median(times) if times else None,
        "reasons": reasons,
        "notes": notes,
        "next_test": next_useful_test(level, reasons),
    }


def next_useful_test(level, reasons):
    if any(
        "identity is incomplete" in reason.lower()
        or "serving application" in reason.lower()
        or "memory headroom" in reason.lower()
        for reason in reasons
    ):
        return "Freeze missing identity/inventory, then plan a new bounded run."
    if any(reason.startswith("Evidence is stale") for reason in reasons):
        return "Re-run the bounded plan against the current target to refresh stale evidence."
    if any("gate did not pass" in reason.lower() for reason in reasons):
        return (
            "Resolve failed readiness or compatibility gates before a new bounded run."
        )
    if any("declared prefix cache" in reason.lower() for reason in reasons):
        return (
            "Correct settings.prefix_cache to what was measured, or find out why the "
            "server does not behave as declared, then plan a new bounded run."
        )
    if any("compaction gate" in reason.lower() for reason in reasons):
        return "Lower harness.compaction_gate.context_tokens or raise reserve_tokens so a compaction is forced, then plan a new bounded run."
    if level == "observed smoke pass":
        return "Run the repeated-task fixture before treating this as qualification."
    if level == "repeated task qualification":
        return "Keep the private evidence with the configuration you deploy, and re-test the deployed combination; this covers only the fixtures tested."
    if reasons:
        return "Resolve the listed reasons before a new bounded run."
    return "Validate a representative operator task before broader use."


def acceptance_rate(metrics):
    """Speculative-decoding draft acceptance, `draft_n_accepted / draft_n`.

    Derived here, when the report is written, and never stored. None when either
    count is missing, when no draft was offered (0 of 0 is neither 0% nor 100%),
    or when the counts contradict each other; the report shows that as unknown.
    """
    offered, accepted = metrics.get("draft_n"), metrics.get("draft_n_accepted")
    if offered is None or accepted is None or offered <= 0 or accepted > offered:
        return None
    return accepted / offered


def report(documents, now=None):
    """The markdown report. Every document is validated once, before any is read."""
    from .tool_schema import NAMESPACE
    from .schema_evaluation import report as schema_report

    for document in documents:
        validate(document)
    schema_docs = [d for d in documents if d["schema"] == NAMESPACE]
    regular = [d for d in documents if d["schema"] != NAMESPACE]
    if schema_docs:
        return (_report(regular, now) + "\n" if regular else "") + schema_report(
            schema_docs
        )
    return _report(regular, now)


def _report(documents, now):
    """The qualification part of `report`, for validated documents."""
    out = [
        "# Poundlock Toolkit qualification report",
        "",
        "Evidence applies only to the frozen fixture and configuration. No universal ranking or production admission is implied.",
        "",
    ]
    for workload in ("extraction", "coding", "investigation"):
        candidates = [(d, _recommendation(d, workload, now)) for d in documents]
        qualified = [
            (d, r) for d, r in candidates if r["level"] == "repeated task qualification"
        ]
        if qualified:
            known = [
                (d, r) for d, r in qualified if r["median_task_seconds"] is not None
            ]
            if len(known) == len(qualified):
                selected = min(known, key=lambda pair: pair[1]["median_task_seconds"])
                out += [
                    f"For {workload}, configuration `{selected[0]['identity_sha256']}` meets the supplied constraints and has the lowest observed median duration among the qualifying supplied runs. This comparison does not account for unknown cost, load time or other machine uses.",
                    "",
                ]
        else:
            out += [
                f"For {workload}: no supplied configuration has sufficient evidence for repeated task qualification.",
                "",
            ]
    for d in documents:
        out += [
            f"## Run {d['run_id']}",
            f"Observed: {d['created_at']}. Configuration: `{d['identity_sha256']}`.",
            f"Backend: {d['backend']}; API: {d['api']}; harness: {d['harness']}.",
            serving_text(d),
            prefix_cache_text(d),
            memory_text(d),
            "",
        ]
        for w in ("extraction", "coding", "investigation"):
            r = _recommendation(d, w, now)
            out += [
                f"### {w}: {r['level']}",
                f"{r['passed']}/{r['attempted']} attempted task runs independently passed; {r['planned']} planned. Median successful task duration: {r['median_task_seconds'] if r['median_task_seconds'] is not None else 'unknown'} seconds.",
                "Outcomes: "
                + (
                    ", ".join(f"{k}={v}" for k, v in r["outcomes"].items())
                    or "untested"
                ),
                "",
            ]
            out.extend("- " + reason for reason in r["reasons"])
            out += [
                "",
                *("Note: " + note for note in r["notes"]),
                "Checks: "
                + ", ".join(f"{k}={v}" for k, v in r["checks"].items())
                + ".",
                "Next required check: " + (r["next_required_check"] or "none"),
                "Next useful test: " + r["next_test"],
                "",
            ]
        out += [
            "| Stage | Workload | Repetition | Outcome | Seconds | First useful output | Decode tokens/s | Draft acceptance |",
            "|---|---|---:|---|---:|---:|---:|---:|",
        ]
        for a in d["attempts"]:
            m = a["metrics"]

            def fmt(k):
                return "unknown" if m[k] is None else f"{m[k]:.4f}"

            rate = acceptance_rate(m)
            drafts = (
                "unknown"
                if rate is None
                else f"{rate:.1%} ({m['draft_n_accepted']:.0f}/{m['draft_n']:.0f})"
            )
            out.append(
                f"| {a['stage']} | {a['workload']} | {a['repetition']} | {a['outcome']} ({a['code']}) | {fmt('wall_seconds')} | {fmt('first_useful_output_seconds')} | {fmt('decode_tokens_per_second')} | {drafts} |"
            )
        out += [
            "",
            "Per-attempt cache state is unknown; the only cache observation is the prefix-cache probe above, when it ran. Attached service load/cold residency, remote placement, cost and any memory the probe did not read remain unknown. No confidence intervals are claimed for these small samples.",
            "",
        ]
    return "\n".join(out).rstrip() + "\n"


def investigation_pair(findable_doc, absent_doc):
    """Both investigation variants, measured against the same identity.

    Refuses to qualify unless each variant independently reaches repeated task
    qualification AND both were measured against the same identity_sha256 --
    a model that always refuses would otherwise "pass" by only ever being
    evaluated on the `absent` variant.
    """
    validate(findable_doc)
    validate(absent_doc)
    findable = recommendation(findable_doc, "investigation")
    absent = recommendation(absent_doc, "investigation")
    identity_match = findable_doc["identity_sha256"] == absent_doc["identity_sha256"]
    qualified = (
        identity_match
        and findable["level"] == "repeated task qualification"
        and absent["level"] == "repeated task qualification"
    )
    return {
        "qualified": qualified,
        "identity_match": identity_match,
        "findable": findable,
        "absent": absent,
    }


SUMMARY_SCHEMA = "urn:poundlock-toolkit:recommendation:1"
WORKLOADS = ("extraction", "coding", "investigation")


def _level(r):
    return {k: r[k] for k in ("level", "passed", "attempted")}


def reasoning_sent(api, settings):
    """What the measured requests told the server about thinking.

    "omitted" sends nothing, so the model's own default applies; "disabled" is
    an explicit off (native Ollama `think: false`, or `reasoning_effort: none`);
    otherwise the effort level sent. `reasoning: off` alone is not enough to
    know: on the OpenAI boundary it omits the field unless
    `reasoning_off_effort` is set.
    """
    if settings["reasoning"] != "off":
        return settings["reasoning"]
    if api == "ollama" or settings.get("reasoning_off_effort") == "none":
        return "disabled"
    return "omitted"


def summary(documents, now=None):
    """The `recommend --format json` document: the report's verdicts as data.

    This is the supported way for another program to read Toolkit's scoring.
    Levels, counts and pairings are decided here, once; a consumer maps them
    and never re-scores attempts. Its shape is data/recommendation.schema.json,
    versioned by `schema`; fields are only added within a version.
    """
    from . import __version__
    from .tool_schema import NAMESPACE

    now = now or datetime.now(timezone.utc)
    results = []
    variants = {}
    for d in documents:
        validate(d)
        row = {
            "result_schema": d["schema"],
            "run_id": d["run_id"],
            "result_sha256": digest(export(d)),
            "identity_sha256": d["identity_sha256"],
            "created_at": d["created_at"],
            "toolkit_version": d["toolkit_version"],
            "synthetic": None,
            "harness": "direct",
            "settings": None,
            "investigation_variant": None,
            "workloads": [],
            "tool_schema": None,
        }
        if d.get("settings"):
            row["settings"] = {
                k: d["settings"][k]
                for k in ("context_tokens", "max_output_tokens", "reasoning")
            }
            # A tool-schema run can use two boundaries; each says its own.
            row["settings"]["reasoning_sent"] = (
                None
                if d["schema"] == NAMESPACE
                else reasoning_sent(d["api"], d["settings"])
            )
        if d["schema"] == NAMESPACE:
            from .schema_evaluation import summaries

            counts = summaries(d)
            row["tool_schema"] = {
                "mode": d["contract"]["mode"],
                "boundaries": [
                    {
                        "boundary": b,
                        **{k: counts[b][k] for k in ("planned", "attempted", "passed")},
                        "reasoning_sent": reasoning_sent(b, d["settings"])
                        if d.get("settings")
                        else None,
                    }
                    for b in d["contract"]["boundaries"]
                ],
            }
        else:
            row["synthetic"] = d["synthetic"]
            row["harness"] = d["harness"]
            for w in WORKLOADS:
                r = recommendation(d, w, now)
                row["workloads"].append(
                    {
                        "workload": w,
                        **_level(r),
                        "planned": r["planned"],
                        "next_required_check": r["next_required_check"],
                    }
                )
            variant = (d.get("investigation") or {}).get("variant")
            planned = any(
                a["stage"] == "task" and a["workload"] == "investigation"
                for a in d["attempts"]
            )
            if variant and planned:
                row["investigation_variant"] = variant
                variants.setdefault(d["identity_sha256"], {}).setdefault(
                    variant, []
                ).append(d)
        results.append(row)

    pairs = []
    for identity, found in sorted(variants.items()):
        if "findable" not in found or "absent" not in found:
            continue
        # The newest run of each variant speaks for the configuration.
        findable = max(found["findable"], key=lambda d: d["created_at"])
        absent = max(found["absent"], key=lambda d: d["created_at"])
        pair = investigation_pair(findable, absent)
        pairs.append(
            {
                "identity_sha256": identity,
                "findable_run_id": findable["run_id"],
                "absent_run_id": absent["run_id"],
                "qualified": pair["qualified"],
                "findable": _level(pair["findable"]),
                "absent": _level(pair["absent"]),
            }
        )
    value = {
        "schema": SUMMARY_SCHEMA,
        "toolkit_version": __version__,
        "evaluated_at": now.isoformat(),
        "results": results,
        "investigation_pairs": pairs,
    }
    validate(value, "recommendation.schema.json")
    return value

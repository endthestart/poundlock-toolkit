"""Measure the prefix cache instead of taking the operator's word for it.

Four unscored requests, sent serially right after the warm-up, in this order:

1. `base`: a prompt, the baseline the rest are compared with;
2. `repeat`: the same prompt again;
3. `agent_turn`: the prompt, the reply the server just returned to it, then a new
   short user message, which is how an agent loop appends a turn after the
   previous reply;
4. `edit_inside`: the prompt with a short suffix inside the same user message.

A server whose prefix cache works reuses nearly all of the prompt on the repeat
and on the agent turn; one whose cache is off, or cannot reuse a prefix for this
model, processes it all again. The edit inside the last message is a different
question: it diverges before the cached reply, so the server has to roll back, which
a hybrid or recurrent model cannot do cheaply. It is a real cost when a harness
edits the tail of a prompt, but it is not evidence that the cache is missing, so
it is judged and reported on its own and never changes the verdict. It runs last
so that whatever the rollback does to the server's state cannot touch the requests
before it.

The evidence for each comparison, strongest first:

1. the server's own count of cached prompt tokens (OpenAI-style
   `usage.prompt_tokens_details.cached_tokens`, or llama.cpp `timings.cache_n`);
2. the server's own prompt-processing time, or failing that the time to first
   token, against the first request's.

The verdict is `yes` or `no` only when both the repeat and the agent turn agree;
anything the evidence does not settle is `inconclusive`, never a guess. The
thresholds are named constants and are stored with the verdict.
"""

from __future__ import annotations

# The probe's requests in the order they are sent, and how many the plan must
# budget for.
REQUESTS = ("base", "repeat", "agent_turn", "edit_inside")
PROBE_REQUESTS = len(REQUESTS)

REQUEST_TEXT = {
    "base": "the base prompt",
    "repeat": "an exact repeat",
    "agent_turn": "an agent turn (the reply, then a new message, appended after it)",
    "edit_inside": "an edit inside the last message",
}

# A share of the prompt served from cache at or above this is reuse; below
# NOT_REUSED it is not. In between (a few chat-template header tokens can match by
# chance, a partial match is a different finding) is not judged.
REUSED_CACHED_FRACTION = 0.5
NOT_REUSED_CACHED_FRACTION = 0.1

# Timing is noisier than a count. The repeat must be at least this much faster to
# count as reuse, and no more than this much faster to count as none.
REUSED_SPEEDUP = 2.0
NOT_REUSED_SPEEDUP = 1.25

# A first request quicker than this is mostly fixed overhead (network, scheduling,
# the first sampled token), so a speedup against it says nothing about the cache.
MIN_MEASURABLE_SECONDS = 0.25

THRESHOLDS = {
    "reused_cached_fraction": REUSED_CACHED_FRACTION,
    "not_reused_cached_fraction": NOT_REUSED_CACHED_FRACTION,
    "reused_speedup": REUSED_SPEEDUP,
    "not_reused_speedup": NOT_REUSED_SPEEDUP,
    "min_measurable_seconds": MIN_MEASURABLE_SECONDS,
}

# Synthetic, fixed lines. Digits tokenize into several tokens each, so a line costs
# roughly 30 tokens; the count is bounded by the context window.
TOKENS_PER_LINE = 30
MIN_LINES, MAX_LINES = 8, 40
EDIT_SUFFIX = " Do not add anything else."
FOLLOW_UP = "Now answer with the single word DONE."


def probe_lines(context_tokens):
    """Prompt length in ledger lines: about a quarter of the window, bounded.

    Long enough that processing it is a measurable cost on real hardware, short
    enough that four requests fit a request's time limit on modest hardware.
    """
    return max(MIN_LINES, min(MAX_LINES, (context_tokens // 4) // TOKENS_PER_LINE))


def probe_prompts(context_tokens, nonce):
    """(base, edited): the second is the first with a small suffix appended.

    Sent as the whole user message, the suffix is an edit inside the last message.
    `nonce` opens the prompt, so a server that still holds an earlier run's cache
    cannot make this run's first request look already cached.
    """
    lines = "\n".join(
        f"Ledger line {n:03d}: crate {n:03d} moved from shelf {(n * 7) % 23 + 1} "
        f"to shelf {(n * 11) % 19 + 1} on day {(n * 5) % 28 + 1}."
        for n in range(1, probe_lines(context_tokens) + 1)
    )
    base = (
        f"Reference {nonce}. Read the ledger below, then answer with the single "
        f"word OK.\n\n{lines}\n\nAnswer with the single word OK."
    )
    return base, base + EDIT_SUFFIX


def probe_request(kind, base, edited, reply):
    """(prompt, earlier messages) of one probe request.

    `reply` is the text the server returned to the request just before, verbatim.
    For the agent turn that is the reply to the base prompt (the repeat's, when the
    server's replies differ from one request to the next), the one the server still
    holds, so the request only appends after it, as an agent loop's next turn does.
    """
    if kind == "agent_turn":
        history = (
            {"role": "user", "content": base},
            {"role": "assistant", "content": reply},
        )
        return FOLLOW_UP, history
    return (edited if kind == "edit_inside" else base), ()


def cached_fraction(metrics):
    """Share of this request's prompt the server served from cache, or None.

    None when the server reported no cached-token count, no usable prompt size,
    or counts that contradict each other (more cached than the prompt held).
    """
    cached, total = metrics.get("cached_tokens"), metrics.get("input_tokens")
    if total is None and cached is not None and metrics.get("prompt_n") is not None:
        total = cached + metrics["prompt_n"]
    if cached is None or not total or cached > total:
        return None
    return cached / total


def comparable_timing(first, later):
    """(basis, first's seconds, later's seconds) from the best timing both have.

    The server's own prompt-processing time is preferred over the client's time to
    first token, which also carries network and scheduling. Two different clocks
    are never compared with each other.
    """
    for name, basis in (
        ("prompt_processing_seconds", "prompt_processing_time"),
        ("first_event_seconds", "first_token_time"),
        ("first_useful_output_seconds", "first_token_time"),
    ):
        if first.get(name) is not None and later.get(name) is not None:
            return basis, first[name], later[name]
    return None


def judge(first, later, warmed):
    """Whether `later` reused `first`'s prompt, from the strongest evidence held.

    Returns one comparison record: `reused` is True, False or None (not settled);
    `basis` names the evidence used, or `conflicting` when a count and a timing
    disagree (a server that always reports zero cached tokens, say).
    """
    fraction = cached_fraction(later)
    by_count = (
        None
        if fraction is None
        else True
        if fraction >= REUSED_CACHED_FRACTION
        else False
        if fraction < NOT_REUSED_CACHED_FRACTION
        else None
    )
    speedup, by_time, time_basis = None, None, None
    clocks = comparable_timing(first, later)
    # Timing is only trusted after a passed warm-up: without one the first request
    # can carry one-off load or compile costs, and a faster repeat would then look
    # like a cache hit.
    if warmed and clocks and clocks[1] >= MIN_MEASURABLE_SECONDS:
        time_basis, was, now = clocks
        speedup = was / max(now, 0.001)
        by_time = (
            True
            if speedup >= REUSED_SPEEDUP
            else False
            if speedup <= NOT_REUSED_SPEEDUP
            else None
        )
    if by_count is not None and by_time is not None and by_count != by_time:
        return {
            "reused": None,
            "basis": "conflicting",
            "cached_fraction": fraction,
            "speedup": speedup,
        }
    if by_count is not None:
        return {
            "reused": by_count,
            "basis": "cached_tokens",
            "cached_fraction": fraction,
            "speedup": speedup,
        }
    return {
        "reused": by_time,
        "basis": time_basis if by_time is not None else None,
        "cached_fraction": fraction,
        "speedup": speedup,
    }


def no_comparison():
    return {"reused": None, "basis": None, "cached_fraction": None, "speedup": None}


# What the edit inside the last message came to, in the words the report uses.
EDIT_VERDICT = {True: "reused", False: "not_reused", None: "inconclusive"}
EDIT_VERDICT_TEXT = {
    "reused": "reused",
    "not_reused": "not reused",
    "inconclusive": "inconclusive",
}


def derive(rows, warmed):
    """The stored verdict from the four probe attempts (in request order).

    `yes` needs reuse on both the exact repeat and the agent turn; `no` needs no
    reuse on both. Reuse on only one, a count that contradicts a timing, or too
    little measured, is `inconclusive` with the reason. The edit inside the last
    message is judged the same way against the first request but only reported
    beside the verdict, as `edit_inside_verdict`: a rollback that is slow, refused
    or timed out leaves the headline alone. A request that did not pass has no
    comparison.
    """

    def compare(kind):
        index = REQUESTS.index(kind)
        if len(rows) <= index or any(
            r["outcome"] != "pass" for r in (rows[0], rows[index])
        ):
            return no_comparison()
        return judge(rows[0]["metrics"], rows[index]["metrics"], warmed)

    repeat, agent_turn, edit_inside = (
        compare(k) for k in ("repeat", "agent_turn", "edit_inside")
    )
    # Everything before the edit settles the verdict, so only those must pass.
    settling = rows[: REQUESTS.index("edit_inside")]
    if len(rows) != PROBE_REQUESTS or any(r["outcome"] != "pass" for r in settling):
        observed, reason = "inconclusive", "probe_incomplete"
    elif "conflicting" in (repeat["basis"], agent_turn["basis"]):
        observed, reason = "inconclusive", "conflicting_evidence"
    elif repeat["reused"] is True and agent_turn["reused"] is True:
        observed, reason = "yes", "reuse_on_repeat_and_agent_turn"
    elif repeat["reused"] is False and agent_turn["reused"] is False:
        observed, reason = "no", "no_reuse_on_repeat_and_agent_turn"
    elif None in (repeat["reused"], agent_turn["reused"]):
        observed, reason = "inconclusive", "insufficient_measurements"
    else:
        observed, reason = "inconclusive", "reuse_on_one_request_only"
    return {
        "observed": observed,
        "reason": reason,
        "warmup_passed": bool(warmed),
        "attempt_ids": [r["id"] for r in rows],
        "repeat": repeat,
        "agent_turn": agent_turn,
        "edit_inside": edit_inside,
        "edit_inside_verdict": EDIT_VERDICT[edit_inside["reused"]],
        "thresholds": dict(THRESHOLDS),
    }


def measured_setting(probe):
    """The `settings.prefix_cache` value a verdict corresponds to, or None.

    Only the verdict counts, so it rests on the repeat and the agent turn. The
    edit inside the last message never does: a server that reuses on repeat and on
    append has a prefix cache, whatever an edit to the tail costs it.
    """
    observed = (probe or {}).get("observed")
    return (
        {"yes": "enabled", "no": "disabled"}.get(observed)
        if isinstance(observed, str)
        else None
    )


def finding(doc):
    """Declared against measured, for reports and the identity check.

    The measured value wins wherever a report needs one value (`effective`); a
    disagreement with a declared setting is `mismatch`. A declared `unknown`
    cannot disagree with anything, and an inconclusive or absent probe leaves the
    declared value standing.
    """
    declared = doc["settings"]["prefix_cache"]
    probe = doc.get("prefix_cache_probe")
    measured = measured_setting(probe)
    return {
        "declared": declared,
        "observed": probe["observed"] if probe else None,
        "measured": measured,
        "effective": measured or declared,
        "mismatch": measured is not None
        and declared != "unknown"
        and declared != measured,
        "probe": probe,
    }


REASON_TEXT = {
    "reuse_on_repeat_and_agent_turn": "the repeat and the agent turn both reused the prompt",
    "no_reuse_on_repeat_and_agent_turn": "neither the repeat nor the agent turn reused the prompt",
    "reuse_on_one_request_only": "only one of the repeat and the agent turn reused the prompt",
    "insufficient_measurements": "the server reported too little to settle both comparisons",
    "conflicting_evidence": "the cached-token count and the timing disagreed",
    "probe_incomplete": "not all four probe requests passed",
}

# Blocks recorded by 0.2.1 rested on the repeat and an extension of the prompt,
# and stay readable as they were.
LEGACY_REASON_TEXT = {
    **REASON_TEXT,
    "reuse_on_repeat_and_extension": "the repeat and the extension both reused the prompt",
    "no_reuse_on_repeat_and_extension": "neither the repeat nor the extension reused the prompt",
    "reuse_on_one_request_only": "only one of the repeat and the extension reused the prompt",
    "probe_incomplete": "not all three probe requests passed",
}


def reason_text(probe):
    """The stored reason in words, for a block of either shape."""
    table = LEGACY_REASON_TEXT if "extension" in probe else REASON_TEXT
    return table[probe["reason"]]


TIME_LABEL = {
    "prompt_processing_time": "server prompt-processing time",
    "first_token_time": "time to first token",
}


def describe(comparison):
    """One comparison in words, from the numbers the verdict rests on.

    A verdict settled by counts is shown by its counts; one settled by timing, by
    the timing it used. Whatever else was measured is shown only when it is all
    there is, or when it was the disagreement.
    """
    basis = comparison["basis"]
    parts = []
    if comparison["cached_fraction"] is not None and basis not in TIME_LABEL:
        parts.append(
            f"{comparison['cached_fraction']:.0%} of the prompt tokens cached (server-reported)"
        )
    speedup = comparison["speedup"]
    if speedup is not None and basis in (*TIME_LABEL, "conflicting", None):
        label = TIME_LABEL.get(basis, "timing")
        parts.append(
            f"{label}: the first request took {speedup:.1f}x as long"
            if speedup >= 1
            else f"{label}: this request took {1 / speedup:.1f}x as long as the first"
        )
    return ", ".join(parts) or "no usable measurement"

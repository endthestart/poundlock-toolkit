# What to test and how to score it

This is the method behind the Toolkit in plain terms: what to run, in what order, and
how to read the results without fooling yourself. The Toolkit automates the short
rungs and helps with the last one. The long ones you run in your own harness; this
page says what to hold them to.

It comes from one operator's runs on a few machines. It is a way of testing, not
scores to copy: every number below is a fact about one setup, not a benchmark.
[FIELD-NOTES.md](FIELD-NOTES.md) records the ones that drove Toolkit features, and
what a Toolkit result does and does not prove is in
[METHODOLOGY.md](METHODOLOGY.md).

## The ladder

Test in rungs. Each costs more than the one under it, and a model only climbs if it
passed the one below.

1. **Quick gates.** Short pass or fail checks: can it be reached and answer, make a
   tool call, fix a small function, find a fact deep in a long context, carry on after
   one forced compaction. They are cheap, so run them on everything. The Toolkit runs
   most of them (readiness, API, tool call, streaming, an extraction smoke test, a
   small coding repair, an opt-in compaction gate). Deep-context recall is not one of
   its workloads; check that yourself.
2. **One real feature, twice.** A real task in your own code, run two times. Expect
   about an hour a trial if the model finishes. One finished in 28 minutes; three others
   ran into their 2.5 to 5 hour caps with nothing to show. So cap every trial. Twice,
   because once cannot tell a good model from a lucky run, and even twice is thin.
3. **The whole brief, overnight.** The full written spec, run unattended. You learn
   whether the model ends its work or just keeps going.
4. **Read the code blind.** Someone reviews the finished code against a fixed rubric
   without knowing which setup wrote it. Of 15 trials that passed all eight automated
   checks, 12 still needed a rewrite.

Three passes in a row is not a ranking. A model that passes only half the time still
goes three for three about one time in eight, so 3 of 3 fits a true pass rate anywhere
from about 44% to 100%. It earns a longer test, not a place on a leaderboard.

**Write the spec first.** The spec decides when a run is done, not the model. It needs
a written brief, tests the agent cannot weaken (it cannot edit or delete them, and the
checker verifies their hashes), real-world constraints, and a result contract: a named
result file and a normal exit. "The agent said it was done" counts for nothing, and a
run still working when the clock stopped did not finish. The Toolkit's coding fixture
is a small version: one function to repair, a verifier the model cannot edit, and a
required result file.

## Strict evidence

The Toolkit runs only a plan you reviewed. The plan freezes the configuration and
states every request and time budget, so a measurement that is not in the plan does
not happen, and a run refuses if anything the plan covered has changed.

Every attempt is written down before it starts, including the ones that fail, time
out or are skipped. A number the server did not report stays empty; the Toolkit never
fills the gap by dividing tokens by seconds. The model's own claim never decides a
pass; an independent check does. A synthetic demo never qualifies anything.

The strongest label, "repeated task qualification", needs three or more verified
passes with every check passing. One verified pass is an "observed smoke pass".
Anything else is "insufficient evidence", with the reasons. Evidence older than 30
days is stale. Settings are what you declared, not what the server confirmed, except
the prefix cache (below).

## Timeouts are not wrong answers

Count "too slow" apart from "wrong". Under a 120-second limit a slow but correct model
can time out on every trial, and a plain pass count shows 0 of 5, the same as a model
that answered wrongly five times. One model went 0 of 5 on a planning call exactly
that way. Another time, 0 of 3 over a 120-second limit became a pass with thinking off,
an 8K output cap and a longer limit; which change mattered was never isolated.

So a timeout is its own outcome and stays in the denominator. The tool-call schema
scenario splits its summary into valid, invalid, timed out and not run. Before a run,
the plan warns when your output cap could not finish inside your time limit at the
speed you require. Read a timeout as "too slow or too tight" and fix the limit before
judging the model.

## Warm-up before timing

The first request after a model loads can pay one-off costs. On a Vulkan build the
graphics driver compiles the model's GPU code for your exact card, like a game
compiling shaders on first launch, then caches it. In one run the first request
read 0.54 tokens a second and the next, a 34.6K-token prompt, ran at 36. It is a few
seconds, once, but inside a scored trial it makes the model look slow.

The Toolkit sends one unscored warm-up before the first scored stage, on by default,
and never counts it. Its timing-based prefix-cache evidence is also trusted only after
a warm-up that passed. If you time things yourself, throw the first request away.

## Speed floors for spills

A spill is a model plus its cache that no longer fits in GPU memory, so part of it runs
from system RAM. It rarely errors. It just slows down. A larger quantization of the
same 27B model, with 22 layers in system RAM, wrote at 6.8 tokens a second against 39.6
for the file that fit, and blind review found no better code. On another machine, the
same model at a larger quantization and 128K context fell to 1.1 to 1.5 tokens a
second in every layer split, with no error.

Set `requirements.min_decode_tokens_per_second` from a speed you measured on a setup
you trust. If the warm speed falls below it, the throughput check fails and the report
names a spill as one possible cause, never a confirmed one; a different backend,
quantization or a busy host looks the same. The floor applies only where the server
reports a decode speed, which is direct extraction. Pi coding runs record none, so the
floor is skipped for them and the report says so.

To look at memory directly, name the local server process in `memory_probe`. The
Toolkit samples its resident memory, and on Linux the used VRAM on each AMD card, while
the run goes. Read the numbers carefully. They are the highest values sampled, so a
lower bound. Process memory is host memory only. The VRAM counter covers everything on
the card, not just your server. On Apple silicon the CPU and GPU share one pool and
none of this shows the GPU's share. A server on another machine stays unknown.

## Record the KV cache and MTP settings

Two settings change speed and fit enough that a result without them cannot be compared.
The KV cache format (`settings.kv_quantization`, say `q4_0`) decides how much memory the
context takes, and so whether a model fits at all; it was part of the winning
configuration and used to be dropped from the evidence. Multi-token prediction
(`settings.speculative`) lets a model draft a few tokens ahead and check them in one
pass. On one 16 GB card it took code writing from 39.6 to 87.0 tokens a second with four
drafts, 78% of them accepted; on Apple silicon it gave nothing. The report works out
the acceptance rate from the counts the server returns.

```json
"settings": {
  "kv_quantization": "q4_0",
  "speculative": { "type": "draft-mtp", "draft_n_max": 4 }
}
```

Leave `speculative` out if you do not know: absent means unrecorded, never "none".

The prefix cache is worth measuring rather than declaring. Set
`limits.prefix_cache_probe_enabled` and the run sends a prompt, the same prompt again,
an agent turn (the prompt, the model's reply, a new message) and the prompt with a
small edit inside its last message, and records what the server says it cached or,
failing that, the timings. The verdict rests on the repeat and the agent turn, which is
how agent loops use the cache. The edit is reported apart: a hybrid model can reuse on
append and still pay a rollback when a harness rewrites the tail of a prompt. The
report shows the measured answer beside the one you declared. If they disagree it says
so, and the run cannot qualify until you fix the declaration.

## The compaction gate

Long agent sessions fill the context, and the agent compacts: it summarizes the history
to make room. A summary can silently drop something said only once, such as the exact
shape of the final report. In one setup 0 of 11 final reports were valid after a
compaction. Every 64K-context trial failed its checks; the 128K trials passed, but only
because nothing had compacted yet.

Put what must survive where compaction cannot reach it, the system prompt. That helped,
but it was a safety net, not a cure. The sturdier fix was structural: one task per fresh
session, ended by an explicit finish, so sessions never grow that far.

`harness.compaction_gate` forces a compaction on purpose, with a small context window,
and counts the ones that completed. A pass counts as gate evidence only if a compaction
really happened; a pass with none is "insufficient evidence", never a pass.

## The blind review packet and rubric

Passing checks is not mergeable: a blind review found blocking defects in every branch
whose checks had passed. A model reviewing its own work does no better. Asked to review
and fix six versions of the code, its own among them, it made 2 better, 3 worse and
left 1 the same, and one "fix" deleted the service while the checks still passed. Two
blind reviewers even disagreed about which branch was best. So use a reviewer that did
not write the code, and do not rest a claim of "better" on one review.

`review-packet` prepares the review from a private list of candidates, each an identity
and the path of a diff file:

```json
{ "candidates": [
    { "identity": "quant-a-trial-1", "diff_path": "diffs/a1.diff" },
    { "identity": "quant-b-trial-1", "diff_path": "diffs/b1.diff" } ] }
```

```sh
poundlock-toolkit review-packet candidates.json --output packet --key-out private/key.json
```

The `packet` folder holds `packet.md`, the diffs under random labels in random order,
and `RUBRIC.md`. Hand the reviewer that folder and nothing else. The key that maps
labels back to identities goes to a different path (its folder must already exist), and
the command refuses a `--key-out` inside the packet folder. It only reads your diffs and
runs nothing.

The rubric asks, per candidate, for a short list of concrete defects, each tagged
BLOCKING or MINOR, and one verdict:

- **merge as-is**: no blocking defects.
- **merge after small fix**: blocking, but narrow and clearly specified.
- **rewrite**: blocking and not narrow, or the change does not do what it claims.

Score every candidate the same way whatever you guess about its origin, and do not let
one verdict anchor the next. Open the key only after every verdict is written down, and
say which of your reviews were blind: the operator's first 24-trial review used one
rubric, but the reviewers could see trial names.

## A worked example

Choosing a setup for one 16 GB graphics card in a Linux box: llama.cpp on Vulkan, 128K
context, `q4_0` KV cache, flash attention on, thinking off, one slot. The candidate is a
27B dense model in two files. These are one operator's numbers from one weekend; they
show how the rungs fit together, not which model to buy.

| Rung | What ran | What the record showed |
|---|---|---|
| Gates | The smaller file (about 3.5 bits a weight, roughly 12 GB), after an unscored warm-up | Tool calls and the small coding repair passed. The stock forced-compaction check failed, then passed once the report contract sat in the system prompt. Code writing ran at 39.6 tokens a second |
| Gates | The larger file, same card and context | It did not fit: 22 layers ran from system RAM, at 6.8 tokens a second. A speed floor set well below the first file's speed catches it |
| One feature, twice | The smaller file | 2 of 2 finished, 28 minutes each. The larger file also finished 2 of 2 but wrote no better code: both were rated rewrite in blind review |
| Whole brief | The smaller file, with MTP at four drafts | 87.0 tokens a second, 78% of drafts accepted. The 15-case brief finished 15 of 15 in 3.2 hours (4.4 without MTP), every case checked by the harness |
| Blind review | Code from these and other trials, under random labels | Across four reviews of the same task, the first not blind, only this model's code was ever rated merge after small fix. Every unattended branch had at least one blocking defect |

The KV format and MTP settings went into the record with every row, which is what lets
the two speeds be compared at all. The last row is the answer that matters. Passing
checks got a candidate this far, and a person still reads the code before it merges.

## Where the exact text lives

This page describes the method. The exact prompts, the verifier cases and the
investigation corpus are in the Toolkit's source (`workloads.py` and the packaged data),
and `review-packet` writes the rubric text. Rungs 2 and 3 are not Toolkit workloads:
run them in your own harness and hold them to the standards above.

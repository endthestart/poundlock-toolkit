# Field notes: what real long-run use taught

Three days of long-run operator use against local hardware surfaced gaps this
toolkit's evidence used to miss silently. This is not a benchmark result and
establishes nothing about any model or device in general: it names the specific
measured facts that drove each change, and only those facts. No number here is
invented; where a fact has no number attached, none is claimed.

The winning configuration found in that use was Qwen3.8 27B dense (IQ3_S), 128K
context, q4_0 KV cache, flash attention on, thinking off, on a llama.cpp Vulkan
build (b11192), one slot. That configuration is a fact about one operator's one
evaluation, not a recommendation Toolkit makes for any other hardware or model.

| Measured fact | Toolkit feature it became |
|---|---|
| The first request after a model load included Vulkan shader compilation and scored as a slow model | One unscored warm-up request per run (per configured boundary for `tool_schema`), sent before the first scored stage or trial and not before each one (a), on by default |
| Multi-token-prediction speculative decoding (4 drafts) raised code-writing throughput 39.6 → 87.0 tok/s, and 36.5 → 63.7 tok/s at a 37K-token context; 78% of drafts were accepted (92% with 2 drafts, at a slightly lower 79.5 tok/s), with identical output | `settings.speculative` (`draft-mtp`/`draft-model`/`none`, `draft_n_max`) and `draft_n`/`draft_n_accepted` metrics, read from llama.cpp's `timings` in both non-streamed and streamed responses, with the acceptance rate derived from them in the report (direct requests only; Pi runs record no timings) (a, d) |
| `kv_quantization` was part of the winning configuration but was silently dropped from recorded evidence | `kv_quantization` kept end-to-end into the qualification document instead of stripped (d) |
| A 128K-context Q4_K_XL model on a 16 GB card spilled out of VRAM: no better code, about 5x slower | Optional `requirements.min_decode_tokens_per_second` and a `throughput` check that names a possible spill as one explanation, never a confirmed cause (b). It applies to the direct extraction workload, the only one that records a server-reported decode speed; Pi coding and investigation runs record none, so the floor is not applied to them and the report notes that |
| The HIP/ROCm container build was slower than Vulkan on the same hardware and crashed outright with MTP enabled | Backend/build identity (already recorded) is what lets two runs on "the same hardware" be told apart when only the serving build differed; no new field needed beyond what serving identity already carries |
| A Metal Mac Pro (W5700X) was about 3.5x slower than the 16 GB AMD Linux box on the same file; adding a second GPU (580X) cost about 30%, not a gain | GPU pools are already recorded and never summed into one allocation; comparative decode-speed evidence (a, b) is what makes a claim like this checkable per configuration instead of asserted |
| An M2 Max laptop (32 GB) measured 11.1 tok/s on code, 7.0 tok/s at a 37K-token context, with other apps open (a lower bound, not a clean-machine number) | Same decode-speed metrics (a); the evidence records the measurement, not whether the machine was otherwise idle -- `inventory`/`identity_complete` is where that caveat belongs operator-side |
| No speculative-decoding gain was observed on Metal (Apple silicon) | `settings.speculative` lets the operator record `"none"` explicitly rather than by omission (Toolkit does not detect it), so a Metal run and a Vulkan run can be told apart in stored evidence instead of both reading as "speculative decoding not mentioned" |

Two lessons did not fit the table because they are about what a single number
cannot show:

**Compaction.** In a long Pi coding session, once the context filled, Pi's own
compaction summarized the history and dropped the instruction for the final
report's exact shape. After compaction, 0 of 11 final reports were valid; every
64K-context trial failed its checks, while the 128K trials passed. The fix that
worked was putting the report contract in the system prompt, which compaction
never touches. `harness.compaction_gate` is the resulting opt-in coding-workload
variant: it sets a small context window and reserve so a compaction is expected,
re-asserts the report contract in the system role on every request via the
fixture's Pi extension, and counts completed compactions into
`metrics.compactions`. The report contract is checked as on every coding run, by
the independent verifier and completion check, now after a forced compaction. A
pass only counts as gate evidence if a compaction actually completed: a gate run
that recorded none is "insufficient evidence" (the compaction gate did not trigger
a compaction), never a pass. The default coding and investigation workloads still
disable compaction, unchanged.

**Blind review.** "Checks pass" is not "mergeable." A blind rubric review, run
once, found blocking defects in every reviewed branch whose automated checks had
passed. The same model reviewing its own prior output made a worse fraction of
cases than it improved -- 2 better, 3 worse, including one "fix" that deleted the
service under review. See [METHODOLOGY.md](METHODOLOGY.md) for the limit this
places on what a passing Toolkit run proves, and `review-packet` (docs/METHODOLOGY.md,
[SPEC.md](SPEC.md)) for the anonymised-packet/rubric/separate-key export this
became.

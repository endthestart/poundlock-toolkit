import json
from .backends import PROBE_MAX_TOKENS
from .cache_probe import PROBE_REQUESTS, REQUEST_TEXT, REQUESTS
from .core import data, digest, identity
from .drift import reviewed_inputs
from .memory_probe import INTERVAL_SECONDS, endpoint_is_local


PI_WORKLOADS = ("coding", "investigation")


def time_feasibility_warnings(max_output_tokens, min_decode_tokens_per_second, limits):
    """Warn when the configured caps cannot fit the run at its stated speed.

    A trial that cannot finish within `request_seconds` reports as a timeout,
    not a task failure -- exactly the confusion (c) exists to separate out in
    the evidence. Catching it here, before execution, is cheaper than reading
    it off a run's summary afterward. `min_decode_tokens_per_second` is a
    stated requirement, or a previously measured speed the operator has fed
    back in as one; absent, no warning is possible.
    """
    if not min_decode_tokens_per_second:
        return []
    expected_seconds = max_output_tokens / min_decode_tokens_per_second
    warnings = []
    if expected_seconds > limits["request_seconds"]:
        warnings.append(
            f"At the required minimum decode speed ({min_decode_tokens_per_second} tok/s), "
            f"generating the configured max_output_tokens ({max_output_tokens}) is expected "
            f"to take about {expected_seconds:.1f}s, exceeding the configured per-request cap "
            f"({limits['request_seconds']}s); trials may report as timeouts rather than task failures."
        )
    if expected_seconds > limits["total_seconds"]:
        warnings.append(
            f"At the required minimum decode speed ({min_decode_tokens_per_second} tok/s), a "
            f"single task is expected to take about {expected_seconds:.1f}s, exceeding the "
            f"configured total budget ({limits['total_seconds']}s); later repetitions may be skipped."
        )
    return warnings


def plan(c, readiness_only=False):
    blockers = []
    pi_workloads = [w for w in c["workloads"] if w in PI_WORKLOADS]
    warmup_enabled = c["limits"].get("warmup_enabled", True)
    cache_probe = c["limits"].get("prefix_cache_probe_enabled", False)
    memory_probe = c.get("memory_probe")
    if memory_probe and not endpoint_is_local(c["endpoint"]):
        blockers.append(
            "memory_probe samples a process on this machine, so it needs a loopback "
            "endpoint (localhost, 127.0.0.1 or ::1); a remote server's memory cannot "
            "be observed from its inference endpoint."
        )
    if (
        c["data_destination"] not in c["requirements"]["allowed_destinations"]
        or c["paid"] is not False
    ):
        blockers.append(
            "Local/private, explicitly unpaid inference is required in release 0.1."
        )
    if c["settings"]["context_tokens"] < c["requirements"]["min_context_tokens"]:
        blockers.append("Configured context does not meet the required minimum.")
    if pi_workloads and (c["harness"]["name"] != "pi" or c["api"] != "openai"):
        blockers.append(
            "Coding and investigation require Pi and its OpenAI-compatible provider path."
        )
    if pi_workloads and not all(
        c["harness"][x] for x in ("image", "network", "version")
    ):
        blockers.append(
            "Coding and investigation need an existing digest-pinned Pi image, version and internal network."
        )
    if pi_workloads and c["credential_env"] is not None:
        blockers.append(
            "Pi v1 needs an unauthenticated endpoint on its isolated network; credential forwarding is not implemented."
        )
    memory = c["inventory"]["memory"]
    inventory = c["inventory"]
    candidates = []
    for entry in json.loads(data("upstream.json")):
        if inventory["os"] == "unknown" or inventory["architecture"] == "unknown":
            state = "unknown: provide authorized OS/architecture inventory"
        elif (
            inventory["os"] in entry["os"]
            and inventory["architecture"] in entry["architecture"]
        ):
            state = "upstream candidate; exact device and model untested"
        else:
            state = "outside this reviewed upstream scope"
        candidates.append(
            {
                "backend": entry["backend"],
                "status": state,
                "prerequisites": entry["support"],
                "source": entry["url"],
                "reviewed_at": entry["reviewed_at"],
            }
        )
    result = {
        "plan_version": 1,
        "config_sha256": digest(c),
        "identity_sha256": digest(identity(c)),
        "reviewed_inputs": reviewed_inputs(c),
        "candidate": {
            "backend": c["backend"]["name"],
            "api": c["api"],
            "harness": c["harness"]["name"],
        },
        "compatibility": "untested; consult support matrix for upstream platform scope",
        "upstream_candidates": candidates,
        "stages": [
            "readiness",
            *(["warmup (unscored)"] if warmup_enabled else []),
            *(["prefix-cache probe (unscored)"] if cache_probe else []),
            "api",
            "tool (coding/investigation only)",
            "performance",
            "task smoke",
            "promoted repetitions",
        ],
        "workloads": c["workloads"],
        "limits": c["limits"],
        "concurrency": 1,
        "retries": 0,
        "download_bytes": 0,
        "maximum_direct_requests": 3
        + (1 if pi_workloads else 0)
        + (1 if warmup_enabled else 0)
        + (PROBE_REQUESTS if cache_probe else 0)
        + (c["limits"]["repetitions"] if "extraction" in c["workloads"] else 0),
        "pi_request_limit_per_task": 12 if pi_workloads else 0,
        "resource_estimate": {
            "weight_bytes_lower_bound": c["model"]["artifact_bytes"],
            "worker_total_writable_mb": c["limits"]["worker_disk_mb"] + 32,
            "kv_cache_bytes": None,
            "runtime_overhead_bytes": None,
            "system_available_bytes": memory["system_available_bytes"],
            "note": "Weight bytes alone cannot establish fit; KV cache, placement and other uses need measurement. GPU pools are never summed.",
        },
        "prerequisites": [
            "Existing endpoint and exact advertised model",
            "Authorized inventory for remote resource claims",
            "For Pi: running Docker, existing pinned image with pi and python3, and existing internal network",
        ],
        "blockers": blockers,
        "warnings": time_feasibility_warnings(
            c["settings"]["max_output_tokens"],
            c["requirements"].get("min_decode_tokens_per_second"),
            c["limits"],
        ),
    }
    result["readiness_only"] = readiness_only
    if readiness_only:
        result.update(
            blockers=[],
            stages=["readiness"],
            workloads=[],
            maximum_direct_requests=1,
            pi_request_limit_per_task=0,
            warnings=[],
        )
    if cache_probe and not readiness_only:
        # Named in the plan and counted in its request budget, like every
        # measurement request: the prompt, the same prompt, an agent turn appended
        # after the reply, then an edit inside the last message.
        result["prefix_cache_probe"] = {
            "requests": PROBE_REQUESTS,
            "sequence": [REQUEST_TEXT[kind] for kind in REQUESTS],
            "scored": False,
            "max_output_tokens": PROBE_MAX_TOKENS,
            "measures": (
                "time to first token and the server's reported cached prompt tokens "
                "and prompt-processing time, where the server reports them"
            ),
            "note": (
                "The verdict rests on the repeat and the agent turn and is stored "
                "beside the declared settings.prefix_cache; a measured yes/no "
                "completes an unknown declaration and a disagreement is reported. "
                "The edit inside the last message is reported separately and never "
                "changes the verdict."
            ),
        }
    if memory_probe and not readiness_only:
        result["memory_probe"] = {
            "target": memory_probe,
            "requests": 0,
            "interval_seconds": INTERVAL_SECONDS,
            "privileged": False,
            "reads": {
                "Linux": [
                    "/proc/<pid>/status: process resident set (VmRSS)",
                    "/sys/class/drm/card*/device/mem_info_vram_used: device-wide AMD VRAM, per card",
                ],
                "Darwin": ["ps -o rss=: process resident set"],
            },
            "note": (
                "Highest sampled value, a lower bound on the true peak. RSS is host "
                "memory only; the VRAM counter covers every process on the card; "
                "Apple-silicon unified memory and other GPUs stay unmeasured. Runs "
                "on the machine Toolkit runs on, which under target-run is the target."
            ),
        }
    if "tool_schema" in c["workloads"]:
        from .tool_schema import contract
        from .core import validate

        validate(c, "config.schema.json")
        if readiness_only:
            raise ValueError(
                "Schema scenarios require their own inference plan; use a separate readiness config"
            )
        spec = contract(c)
        requests_per_boundary = spec["trials_per_boundary"] + (
            1 if spec["warmup_enabled"] else 0
        )
        result.update(
            stages=["tool_schema"],
            tool_schema=spec,
            maximum_direct_requests=requests_per_boundary * len(spec["boundaries"]),
            pi_request_limit_per_task=0,
            prerequisites=[
                "Existing direct inference endpoint(s) with the same explicitly selected model",
                "Tool calling supported at each configured boundary; unsupported responses count as failures",
            ],
        )
    if "investigation" in c["workloads"]:
        from .fixtures import INVESTIGATION_FIXTURE

        fx = INVESTIGATION_FIXTURE(c["investigation"]["variant"])
        result["investigation"] = {
            "variant": fx.variant,
            "corpus_sha256": fx.corpus_sha256,
            "entry_count": fx.entry_count,
        }
    return result

"""Bounded attached OpenAI/Ollama adapters, adapted from earlier research probes."""

import json
import os
import time
import urllib.error
import urllib.request
from .core import Failure, digest, numeric
from .workloads import TOOL

METRIC_NAMES = [
    "wall_seconds",
    "first_event_seconds",
    "first_useful_output_seconds",
    "load_seconds",
    "prompt_processing_seconds",
    "decode_seconds",
    "prefill_tokens_per_second",
    "decode_tokens_per_second",
    "input_tokens",
    "output_tokens",
    "peak_process_rss_bytes",
    "prompt_n",
    "predicted_n",
    "draft_n",
    "draft_n_accepted",
    "compactions",
    "loops_detected",
    "cached_tokens",
]

# The prefix-cache probe asks for a token or two, not an answer: it measures how
# a prompt is processed, so the reply length must not dominate the timing.
PROBE_MAX_TOKENS = 8


def chat_messages(prompt, history=()):
    """The request's messages: any earlier turns, then this user message."""
    return [*history, {"role": "user", "content": prompt}]


def read_timings(metrics, timing):
    """llama.cpp's `timings` object, kept verbatim where the toolkit has a slot.

    Present on both non-streamed responses and, on servers new enough to
    report it, the final streamed chunk. `draft_n`/`draft_n_accepted` are
    speculative-decoding counters (MTP or a draft model); absent when
    speculative decoding is not configured, never synthesized.
    """
    metrics["decode_tokens_per_second"] = numeric(timing.get("predicted_per_second"))
    metrics["prefill_tokens_per_second"] = numeric(timing.get("prompt_per_second"))
    metrics["prompt_n"] = numeric(timing.get("prompt_n"))
    metrics["predicted_n"] = numeric(timing.get("predicted_n"))
    metrics["draft_n"] = numeric(timing.get("draft_n"))
    metrics["draft_n_accepted"] = numeric(timing.get("draft_n_accepted"))
    # llama.cpp reports the server-processed prompt time and, on builds new enough,
    # how many prompt tokens came from the slot's cache. The OpenAI-style count
    # (read_usage) wins when a server sends both.
    prompt_ms = numeric(timing.get("prompt_ms"))
    metrics["prompt_processing_seconds"] = (
        prompt_ms / 1000 if prompt_ms is not None else None
    )
    if metrics.get("cached_tokens") is None:
        metrics["cached_tokens"] = numeric(timing.get("cache_n"))
    return metrics


def read_usage(metrics, usage):
    """OpenAI-style `usage`, kept as reported.

    `prompt_tokens_details.cached_tokens` is the server's own count of prompt
    tokens served from its prefix cache. Absent stays null: it is never guessed
    from a faster response.
    """
    metrics["input_tokens"] = numeric(usage.get("prompt_tokens"))
    metrics["output_tokens"] = numeric(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and numeric(details.get("cached_tokens")) is not None:
        metrics["cached_tokens"] = numeric(details["cached_tokens"])
    return metrics


def empty_metrics():
    return dict.fromkeys(METRIC_NAMES)


def reasoning_effort(settings):
    """OpenAI-boundary reasoning_effort, or None to omit the field."""
    if settings["reasoning"] != "off":
        return settings["reasoning"]
    return settings.get("reasoning_off_effort")


def openai_sampling(settings, max_tokens=None):
    """OpenAI-boundary sampling fields; `max_tokens` overrides the output budget."""
    return {
        "temperature": settings["temperature"],
        "max_tokens": max_tokens or settings["max_output_tokens"],
    }


def ollama_options(settings, max_tokens=None):
    """Native Ollama `options`; `max_tokens` overrides the output budget."""
    return {
        "temperature": settings["temperature"],
        "num_predict": max_tokens or settings["max_output_tokens"],
        "num_ctx": settings["context_tokens"],
    }


def ollama_think(settings):
    """Native Ollama `think`, a dialect of its own: false, or the chosen level."""
    return False if settings["reasoning"] == "off" else settings["reasoning"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Failure("protocol_failure", "http_error")


class Backend:
    def __init__(self, config):
        self.c = config
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, path, payload=None):
        c = self.c
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if c["credential_env"]:
            key = os.environ.get(c["credential_env"])
            if not key:
                raise Failure("infrastructure_failure", "unavailable")
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(
            c["endpoint"].rstrip("/") + path,
            data=None
            if payload is None
            else json.dumps(payload, allow_nan=False).encode(),
            headers=headers,
        )
        try:
            return self.opener.open(req, timeout=c["limits"]["request_seconds"])
        except urllib.error.HTTPError as exc:
            exc.close()
            raise Failure(
                "infrastructure_failure"
                if exc.code >= 500 or exc.code in (401, 403, 408, 429)
                else "protocol_failure",
                "http_error",
            ) from None
        except (urllib.error.URLError, OSError):
            raise Failure("infrastructure_failure", "unavailable") from None

    def json_response(self, path, payload=None):
        with self.request(path, payload) as response:
            raw = response.read(self.c["limits"]["response_bytes"] + 1)
        if len(raw) > self.c["limits"]["response_bytes"]:
            raise Failure("protocol_failure", "response_limit")
        try:
            value = json.loads(raw)
            if type(value) is not dict:
                raise ValueError()
            return value, digest(raw)
        except (ValueError, UnicodeError):
            raise Failure("protocol_failure", "malformed_response") from None

    def readiness(self):
        v, sha = self.json_response(
            "/models" if self.c["api"] == "openai" else "/api/tags"
        )
        try:
            rows = v["data"] if self.c["api"] == "openai" else v["models"]
            if not isinstance(rows, list):
                raise TypeError()
            names = [x["id"] if self.c["api"] == "openai" else x["name"] for x in rows]
        except (TypeError, KeyError):
            raise Failure("protocol_failure", "malformed_response") from None
        model = self.c["model"]["id"]
        if model not in names:
            raise Failure("protocol_failure", "model_not_listed")
        return {"metrics": empty_metrics(), "response_sha256": sha}

    def probe(self, prompt, history=()):
        """One unscored prefix-cache probe request.

        `history` is the turns before `prompt`, so a probe can look like a later
        turn of a conversation. A very short reply, and a reply cut off by that
        limit is fine: only the prompt's processing is measured. OpenAI-compatible
        servers are streamed so the time to first token is real; native Ollama has
        no streamed path here and reports its own prompt-processing time instead.
        """
        return self.complete(
            prompt,
            stream=self.c["api"] == "openai",
            max_tokens=PROBE_MAX_TOKENS,
            truncation_ok=True,
            history=history,
        )

    def complete(
        self,
        prompt,
        *,
        tool=False,
        stream=False,
        max_tokens=None,
        truncation_ok=False,
        history=(),
    ):
        if self.c["api"] == "ollama":
            return self.ollama(prompt, tool, max_tokens, truncation_ok, history)
        settings = self.c["settings"]
        payload = {
            "model": self.c["model"]["id"],
            "messages": chat_messages(prompt, history),
            **openai_sampling(settings, max_tokens),
            "stream": stream,
        }
        if (effort := reasoning_effort(settings)) is not None:
            payload["reasoning_effort"] = effort
        if tool:
            payload.update(tools=[TOOL], tool_choice="required")
        if stream:
            payload["stream_options"] = {"include_usage": True}
            return self.streaming(
                payload, ("stop", "length") if truncation_ok else ("stop",)
            )
        started = time.monotonic()
        value, sha = self.json_response("/chat/completions", payload)
        metrics = empty_metrics()
        metrics["wall_seconds"] = time.monotonic() - started
        try:
            choice = value["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if not isinstance(text, str) or not isinstance(calls, list):
                raise TypeError()
            if choice["finish_reason"] not in (
                ("stop", "tool_calls", "length")
                if truncation_ok
                else ("stop", "tool_calls")
            ):
                raise Failure("completion_failure", "missing_completion")
            read_usage(metrics, value.get("usage") or {})
            # llama.cpp server-reported timing, never wall-rate substitution.
            read_timings(metrics, value.get("timings") or {})
        except (KeyError, IndexError, TypeError, AttributeError):
            raise Failure("protocol_failure", "malformed_response") from None
        return {
            "text": text,
            "calls": calls,
            "metrics": metrics,
            "response_sha256": sha,
        }

    def streaming(self, payload, finish_ok=("stop",)):
        started = time.monotonic()
        metrics = empty_metrics()
        fragments, raw_chunks = [], []
        finish, done, total = False, False, 0
        try:
            with self.request("/chat/completions", payload) as response:
                while True:
                    raw = response.readline(self.c["limits"]["response_bytes"] + 1)
                    if not raw:
                        break
                    total += len(raw)
                    if total > self.c["limits"]["response_bytes"]:
                        raise Failure("protocol_failure", "response_limit")
                    raw_chunks.append(raw)
                    line = raw.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        done = True
                        break
                    if not body:
                        continue
                    event = json.loads(body)
                    if event.get("error"):
                        raise Failure("protocol_failure", "malformed_response")
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        meaningful = (
                            content
                            or delta.get("reasoning_content")
                            or delta.get("tool_calls")
                        )
                        if meaningful and metrics["first_event_seconds"] is None:
                            metrics["first_event_seconds"] = time.monotonic() - started
                        if content:
                            if not isinstance(content, str):
                                raise TypeError()
                            if metrics["first_useful_output_seconds"] is None:
                                metrics["first_useful_output_seconds"] = (
                                    time.monotonic() - started
                                )
                            fragments.append(content)
                        if choice.get("finish_reason") is not None:
                            if choice["finish_reason"] not in finish_ok:
                                raise Failure(
                                    "completion_failure", "missing_completion"
                                )
                            finish = True
                    if event.get("usage"):
                        read_usage(metrics, event["usage"])
                    # llama.cpp servers new enough to report per-request timings put
                    # them on the final streamed chunk, alongside `usage`.
                    if event.get("timings"):
                        read_timings(metrics, event["timings"])
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise Failure("protocol_failure", "malformed_response") from None
        if not finish or not done:
            raise Failure("completion_failure", "missing_completion")
        metrics["wall_seconds"] = time.monotonic() - started
        return {
            "text": "".join(fragments),
            "calls": [],
            "metrics": metrics,
            "response_sha256": digest(b"".join(raw_chunks)),
        }

    def ollama(self, prompt, tool, max_tokens=None, truncation_ok=False, history=()):
        s = self.c["settings"]
        payload = {
            "model": self.c["model"]["id"],
            "messages": chat_messages(prompt, history),
            "stream": False,
            "options": ollama_options(s, max_tokens),
            "think": ollama_think(s),
        }
        if tool:
            payload["tools"] = [TOOL]
        started = time.monotonic()
        v, sha = self.json_response("/api/chat", payload)
        m = empty_metrics()
        m["wall_seconds"] = time.monotonic() - started
        try:
            if v.get("done") is not True or v.get("done_reason") not in (
                ("stop", "length", None) if truncation_ok else ("stop", None)
            ):
                raise Failure("completion_failure", "missing_completion")
            text, calls = (
                v["message"].get("content") or "",
                v["message"].get("tool_calls") or [],
            )
            if not isinstance(text, str) or not isinstance(calls, list):
                raise TypeError()
            for output, source in (
                ("load_seconds", "load_duration"),
                ("prompt_processing_seconds", "prompt_eval_duration"),
                ("decode_seconds", "eval_duration"),
            ):
                n = numeric(v.get(source))
                m[output] = n / 1e9 if n is not None else None
            m["input_tokens"], m["output_tokens"] = (
                numeric(v.get("prompt_eval_count")),
                numeric(v.get("eval_count")),
            )
            for count, seconds, rate in (
                (
                    "input_tokens",
                    "prompt_processing_seconds",
                    "prefill_tokens_per_second",
                ),
                ("output_tokens", "decode_seconds", "decode_tokens_per_second"),
            ):
                if m[count] is not None and m[seconds] is not None and m[seconds] > 0:
                    m[rate] = m[count] / m[seconds]
        except (TypeError, KeyError, AttributeError):
            raise Failure("protocol_failure", "malformed_response") from None
        return {"text": text, "calls": calls, "metrics": m, "response_sha256": sha}

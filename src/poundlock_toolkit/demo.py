"""Offline synthetic transport for exercising the workflow; never qualification."""

import json
from .backends import empty_metrics
from .core import digest
from .workloads import EXPECTED


class SyntheticBackend:
    def readiness(self):
        return {
            "metrics": empty_metrics(),
            "response_sha256": digest(b"synthetic-readiness"),
        }

    def complete(self, prompt, tool=False, stream=False):
        text = "OK" if prompt == "Reply exactly OK" else json.dumps(EXPECTED)
        m = empty_metrics()
        m["wall_seconds"] = 0.25
        return {
            "text": text,
            "calls": [],
            "metrics": m,
            "response_sha256": digest(text),
        }

    def probe(self, prompt, history=()):
        m = empty_metrics()
        m["wall_seconds"] = 0.25
        return {
            "text": "OK",
            "calls": [],
            "metrics": m,
            "response_sha256": digest(prompt),
        }

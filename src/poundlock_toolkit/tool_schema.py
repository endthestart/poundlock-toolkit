"""Fixed nested planning fixture and strict, content-free argument diagnostics."""

import json
import math
from itertools import islice
from jsonschema import Draft202012Validator
from .core import data, digest

NAMESPACE = "urn:poundlock-toolkit:tool-schema:1"
SCHEMA = json.loads(data("propose-plan.schema.json"))
TOOL = {
    "type": "function",
    "function": {
        "name": "propose_plan",
        "description": "Propose a bounded project plan with typed nested tasks.",
        "parameters": SCHEMA,
    },
}
TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    type(None): "null",
}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite number")
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_float=finite,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite number")),
    )


def typename(value):
    return TYPES.get(type(value), "unknown")


def fixture(c):
    spec = c["tool_schema"]
    return spec.get(
        "conversation", json.loads(data("tool-schema-" + spec["mode"] + ".json"))
    )


def contract(c):
    messages = fixture(c)
    return {
        "fixture_sha256": digest(TOOL),
        "conversation_sha256": digest(messages),
        "conversation_source": "operator"
        if "conversation" in c["tool_schema"]
        else "synthetic",
        "mode": c["tool_schema"]["mode"],
        "trials_per_boundary": c["tool_schema"]["trials"],
        "boundaries": sorted(c["tool_schema"]["endpoints"]),
        "prompt_characters": len(json.dumps(messages)) + len(json.dumps(TOOL)),
        "warmup_enabled": c["tool_schema"].get("warmup_enabled", True),
    }


def safe_path(path):
    """Validator paths are untrusted; expose only schema-known names and indices."""
    allowed = set(SCHEMA["properties"]) | set(
        SCHEMA["properties"]["tasks"]["items"]["properties"]
    )
    return "$" + "".join(
        f"[{p}]" if type(p) is int else "." + (p if p in allowed else "<unknown>")
        for p in path
    )


def score(arguments):
    result = {
        "argument_representation": typename(arguments),
        "outer_parse_success": False,
        "schema_valid": False,
        "errors": [],
        "errors_truncated": False,
    }
    value = arguments
    try:
        if isinstance(arguments, str):
            value = strict_json(arguments)
        else:
            # Native object arguments still must be finite JSON values.
            json.dumps(value, allow_nan=False)
        result["outer_parse_success"] = True
    except (ValueError, TypeError, UnicodeError, RecursionError):
        result["errors"] = [
            {
                "path": "$",
                "keyword": "parse",
                "expected": "object",
                "actual": typename(arguments),
                "nested_json": None,
            }
        ]
        return result
    errors = []
    visited = 0
    for error in islice(Draft202012Validator(SCHEMA).iter_errors(value), 65):
        visited += 1
        path = list(error.absolute_path)
        expected = error.schema.get(
            "type", "string" if error.validator == "enum" else "object"
        )
        nested = None
        if (
            error.validator == "type"
            and isinstance(error.instance, str)
            and expected in ("array", "object")
        ):
            try:
                nested = "valid_" + typename(strict_json(error.instance))
            except (ValueError, UnicodeError, RecursionError):
                nested = "malformed"
        if error.validator == "required":
            # Derive missing known fields, never use validator's rendered message.
            for key in error.validator_value:
                if key not in error.instance:
                    errors.append(
                        {
                            "path": safe_path(path + [key]),
                            "keyword": "required",
                            "expected": error.schema["properties"][key].get(
                                "type", "string"
                            ),
                            "actual": "missing",
                            "nested_json": None,
                        }
                    )
        else:
            errors.append(
                {
                    "path": safe_path(path),
                    "keyword": error.validator,
                    "expected": expected,
                    "actual": typename(error.instance),
                    "nested_json": nested,
                }
            )
        errors = list({json.dumps(e, sort_keys=True): e for e in errors}.values())
        if len(errors) > 64:
            break
    # Deduplicate required errors while preserving deterministic traversal order.
    unique = list({json.dumps(e, sort_keys=True): e for e in errors}.values())
    result.update(
        schema_valid=not unique,
        errors=unique[:64],
        errors_truncated=len(unique) > 64 or visited == 65,
    )
    return result

"""Target workflow presentation; all model evaluation stays in evaluation.py."""

import json
from pathlib import Path
from .core import atomic_json, config, read_json
from .memory_probe import endpoint_is_local
from .targets import control, inventory, save_result, start, target_config, target_plan


def arguments(sub):
    p = sub.add_parser(
        "target-inspect", help="Read-only inventory from a local or SSH target"
    )
    p.add_argument("target")
    p.add_argument("--output", required=True)
    p = sub.add_parser(
        "target-config",
        help="Bind observed target inventory to an existing loopback endpoint configuration",
    )
    p.add_argument("target")
    p.add_argument("config")
    p.add_argument("--output", required=True)
    p = sub.add_parser(
        "target-plan", help="Bind a bounded evaluation to the inspected target/package"
    )
    p.add_argument("target")
    p.add_argument("config")
    p.add_argument("--output", required=True)
    p.add_argument("--readiness-only", action="store_true")
    p = sub.add_parser("target-run", help="Attach to one finite local/SSH evaluation")
    p.add_argument("target")
    p.add_argument("config")
    p.add_argument("--plan", required=True)
    p.add_argument("--receipt", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--execute", action="store_true")
    for command in ("target-status", "target-cancel", "target-remove"):
        p = sub.add_parser(command)
        p.add_argument("target")
        p.add_argument("receipt")
        p.add_argument("--output")
        if command != "target-status":
            p.add_argument("--execute", action="store_true")


def run(args):
    target = target_config(args.target)
    if args.command == "target-inspect":
        result = inventory(target)
    elif args.command == "target-config":
        result = config(args.config)
        endpoints = [
            result["endpoint"],
            *result.get("tool_schema", {}).get("endpoints", {}).values(),
        ]
        if not all(endpoint_is_local(endpoint) for endpoint in endpoints):
            raise ValueError(
                "Observed target inventory only applies to target loopback endpoints; use authorized operator inventory otherwise"
            )
        observed = inventory(target)["inventory"]
        result["inventory"] = {
            "source": "observed",
            "os": observed["os"],
            "architecture": observed["architecture"],
            "memory": observed["memory"],
        }
    elif args.command == "target-plan":
        result = target_plan(config(args.config), target, args.readiness_only)
    elif args.command == "target-run":
        reviewed = read_json(args.plan)
        print(json.dumps(reviewed, indent=2), flush=True)
        if not args.execute:
            print(
                json.dumps(
                    {
                        "state": "pending_consent",
                        "next": "Review target plan and use --execute",
                    }
                )
            )
            return 3
        if Path(args.output).exists():
            raise ValueError("Output already exists")
        result = start(config(args.config), target, reviewed, args.receipt)
        save_result(result, args.output)
        print(
            json.dumps(
                {"state": result["state"], "attach_checks": result["attach_checks"]},
                indent=2,
            )
        )
        return 0 if result["state"]["status"] == "completed" else 1
    else:
        operation = args.command.removeprefix("target-")
        if operation != "status" and not args.execute:
            print(json.dumps({"state": "pending_consent", "operation": operation}))
            return 3
        result = control(target, read_json(args.receipt), operation)
        if args.output:
            save_result(result, args.output)
        print(json.dumps({k: v for k, v in result.items() if k != "result"}, indent=2))
        return 0
    if Path(args.output).exists():
        raise ValueError("Output already exists")
    atomic_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0

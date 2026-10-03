"""Command-line interface; no host dependencies beyond Python and Docker."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import fields
from pathlib import Path

from . import __version__
from .harnesses import HARNESS_VERSIONS
from .report import write_reports
from .runner import BASE_IMAGE, Docker, RunConfig, normalize_base_url, probe_endpoint, run_benchmark
from .task_sets import TASK_SETS
from .tasks import TASKS


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def optional_timeout(value: str) -> float | None:
    if value.strip().lower() == "unlimited":
        return None
    return positive_float(value)


def optional_log_limit(value: str) -> int | None:
    if value.strip().lower() == "unlimited":
        return None
    return positive_int(value)


def nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="agent-bench",
        description=(
            "Compare coding harnesses against ONE model on a LAN llama.cpp/OpenAI-compatible server. "
            "The host orchestrates; all agent execution and grading happen in Docker."
        ),
    )
    root.add_argument("--version", action="version", version=f"agent-bench {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "list", help="List built-in tasks, task sets, and harness versions"
    )
    listing.add_argument(
        "--json", action="store_true", help="Emit machine-readable task and task-set descriptions"
    )

    run = commands.add_parser(
        "run",
        help="Run all selected harnesses against a single model",
        argument_default=argparse.SUPPRESS,
    )
    run.add_argument(
        "--config", metavar="FILE", help="JSON RunConfig defaults; CLI options override"
    )
    run.add_argument(
        "--model", metavar="ID", help="Required served model ID (or set model in --config)"
    )
    run.add_argument(
        "--base-url",
        "--llama-url",
        dest="base_url",
        metavar="URL",
        help="llama.cpp server URL; /v1 appended if missing (default: http://host.docker.internal:8080/v1)",
    )
    run.add_argument(
        "--harnesses",
        nargs="+",
        choices=list(HARNESS_VERSIONS),
        help="Default: all three harnesses",
    )
    run.add_argument(
        "--task-sets",
        nargs="+",
        choices=list(TASK_SETS),
        help="One or more task sets; overlaps run once (default: default, unless --tasks is supplied)",
    )
    run.add_argument(
        "--tasks",
        nargs="+",
        choices=list(TASKS),
        help="Individual tasks; combine with --task-sets to add tasks to their union",
    )
    run.add_argument(
        "--output",
        metavar="DIR",
        help="Parent output directory (default: results); creates a unique run subdirectory",
    )
    run.add_argument(
        "--repeats", type=positive_int, help="Fresh attempts per task/harness (default: 1)"
    )
    run.add_argument(
        "--timeout",
        type=optional_timeout,
        metavar="SECONDS|unlimited",
        help="Agent wall time limit in seconds, or unlimited (default: 600)",
    )
    run.add_argument(
        "--grade-timeout",
        type=positive_float,
        help="Grading wall time limit in seconds (default: 60)",
    )
    run.add_argument(
        "--build-timeout",
        type=positive_float,
        help="Limit per image build in seconds (default: 900)",
    )
    run.add_argument(
        "--cpus", type=positive_float, help="Docker CPU limit for agents and graders (default: 2)"
    )
    run.add_argument(
        "--memory", help="Docker memory and swap limit, e.g. 4g (default: 4g; no additional swap)"
    )
    run.add_argument("--pids-limit", type=positive_int, help="Container PID limit (default: 256)")
    run.add_argument(
        "--context-window",
        type=positive_int,
        help="Model context capacity; match server --ctx-size (default: 32768)",
    )
    run.add_argument(
        "--max-tokens",
        type=positive_int,
        help="Per-response output token limit (default: 4096); not a run budget",
    )
    run.add_argument(
        "--api-profile",
        choices=["llama-cpp", "openai-compatible"],
        help="Request dialect (default: llama-cpp); maps --thinking to chat_template_kwargs.enable_thinking",
    )
    run.add_argument(
        "--reasoning",
        action=argparse.BooleanOptionalAction,
        help="Declare a reasoning model (default: false)",
    )
    run.add_argument(
        "--thinking",
        choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort where supported (default: off); requires --reasoning",
    )
    run.add_argument(
        "--tool-profile",
        choices=["native", "common"],
        help="native: harness-specific tools; common: read/write/edit/bash only (default: native)",
    )
    run.add_argument(
        "--seed", type=int, help="Seed for reproducible shuffled SERIAL scheduling (default: 42)"
    )
    run.add_argument(
        "--api-key-env",
        metavar="NAME",
        help="Host environment variable for optional server API key (default: AGENT_BENCH_API_KEY)",
    )
    run.add_argument(
        "--input-price",
        type=nonnegative_float,
        help="Optional USD per million input tokens; unknown by default",
    )
    run.add_argument(
        "--output-price",
        type=nonnegative_float,
        help="Optional USD per million output tokens; unknown by default",
    )
    run.add_argument(
        "--network",
        help="Docker agent network: bridge, host, or a named network (default: bridge); graders always none",
    )
    run.add_argument(
        "--platform",
        choices=["linux/amd64", "linux/arm64"],
        help="Explicit image/container platform; default native",
    )
    run.add_argument(
        "--no-build",
        dest="build",
        action="store_false",
        help="Use existing local images instead of building",
    )
    run.add_argument(
        "--harness-version",
        action="append",
        metavar="NAME=VERSION",
        help="Override a pinned harness version; repeatable",
    )
    run.add_argument(
        "--max-log-bytes",
        type=optional_log_limit,
        metavar="BYTES|unlimited",
        help="Per stdout/stderr limit, or unlimited; exceeding a finite limit aborts (default: 2000000)",
    )
    run.add_argument(
        "--max-source-bytes",
        type=positive_int,
        help="Maximum submitted Python source bytes (default: 2000000)",
    )

    build = commands.add_parser("build", help="Build the common dev image and harness images")
    build.add_argument(
        "--harnesses", nargs="+", choices=list(HARNESS_VERSIONS), default=list(HARNESS_VERSIONS)
    )
    build.add_argument("--harness-version", action="append", default=[], metavar="NAME=VERSION")
    build.add_argument("--platform", choices=["linux/amd64", "linux/arm64"])
    build.add_argument("--build-timeout", type=positive_float, default=900)

    doctor = commands.add_parser(
        "doctor", help="Check Docker and list model IDs exposed by the server"
    )
    doctor.add_argument(
        "--base-url", "--llama-url", dest="base_url", default="http://host.docker.internal:8080/v1"
    )
    doctor.add_argument("--model", help="Optional ID to check against /v1/models")
    doctor.add_argument("--api-key-env", default="AGENT_BENCH_API_KEY")
    doctor.add_argument("--timeout", type=positive_float, default=10)
    doctor.add_argument("--network", default="bridge")
    doctor.add_argument("--platform", choices=["linux/amd64", "linux/arm64"])

    render = commands.add_parser(
        "report", help="Regenerate a standalone HTML/JSON pair from results.json"
    )
    render.add_argument("json_file", type=Path)
    render.add_argument("--output", type=Path, help="Default: directory containing the JSON file")
    return root


def versions(overrides: list[str], existing: dict | None = None) -> dict[str, str]:
    result = dict(HARNESS_VERSIONS)
    if existing:
        if not isinstance(existing, dict) or any(name not in HARNESS_VERSIONS for name in existing):
            raise ValueError("harness_versions must map supported harness names to versions")
        result.update(existing)
    for item in overrides:
        name, separator, version = item.partition("=")
        if not separator or name not in HARNESS_VERSIONS or not version:
            raise ValueError("Use --harness-version NAME=VERSION with a supported harness name")
        result[name] = version
    return result


def run_configuration(arguments: argparse.Namespace) -> RunConfig:
    cli = vars(arguments).copy()
    cli.pop("command")
    config_file = cli.pop("config", None)
    values = {}
    if config_file:
        with Path(config_file).open(encoding="utf-8") as stream:
            values = json.load(stream)
        if not isinstance(values, dict):
            raise ValueError("Config must be a JSON object")
        allowed = {f.name for f in fields(RunConfig)}
        unknown = values.keys() - allowed
        if unknown:
            raise ValueError(f"Unknown config options: {', '.join(sorted(unknown))}")
    overrides = cli.pop("harness_version", [])
    # Explicit CLI selections replace the file's entire selection, not just one
    # half of it. Both CLI options together still form a union.
    if "tasks" in cli or "task_sets" in cli:
        values.pop("tasks", None)
        values.pop("task_sets", None)
    values.update(cli)
    values["harness_versions"] = versions(overrides, values.get("harness_versions"))
    if not values.get("model"):
        raise ValueError("Provide --model with the server's model ID (or model in --config)")
    return RunConfig(**values).validate()


def doctor_check(arguments: argparse.Namespace) -> dict:
    config = RunConfig(
        model=arguments.model or "doctor",
        base_url=normalize_base_url(arguments.base_url),
        network=arguments.network,
        platform=arguments.platform,
    ).validate()
    docker = Docker(config)
    result = {"docker": docker.available(), "base_url": config.base_url}
    # Probe from the host when the URL is directly reachable. host.docker.internal
    # is a Docker-side alias, not reliably a host DNS name; use localhost on host.
    host_url = config.base_url.replace("host.docker.internal", "localhost")
    try:
        result["host_endpoint"] = probe_endpoint(
            host_url, arguments.model, arguments.api_key_env, arguments.timeout
        )
    except (OSError, ValueError) as exc:
        result["host_endpoint"] = {"error": str(exc)}
    try:
        base = docker.image_metadata(BASE_IMAGE)
    except RuntimeError:
        result["container_endpoint"] = {
            "checked": False,
            "note": "Build images first: agent-bench build",
        }
        result["ok"] = "error" not in result["host_endpoint"]
        return result
    # Trusted inline stdlib code, never model-generated. Checks Docker routing,
    # /models and selected ID, but doesn't bill an inference or test tool calling.
    script = (
        "import json, os, urllib.request; "
        "r=urllib.request.Request(os.environ['BENCH_URL']+'/models', "
        "headers={'Authorization':'Bearer '+os.environ['BENCH_API_KEY']}); "
        "data=json.load(urllib.request.urlopen(r,timeout=10)); "
        "print(json.dumps([m['id'] for m in data.get('data',[])]))"
    )
    check = docker.execute(
        image=base["id"],
        mounts=[],
        command=["python", "-I", "-c", script],
        timeout=arguments.timeout + 5,
        environment={
            "BENCH_URL": config.base_url,
            "BENCH_API_KEY": os.environ.get(arguments.api_key_env) or "local-no-key",
        },
    )
    if check.returncode or check.timed_out:
        result["container_endpoint"] = {
            "checked": True,
            "ok": False,
            "error": check.stderr or "Container request timed out",
        }
        result["ok"] = False
    else:
        ids = json.loads(check.stdout)
        result["container_endpoint"] = {
            "checked": True,
            "models": ids,
            "model_present": arguments.model in ids if arguments.model else None,
        }
        result["ok"] = arguments.model in ids if arguments.model else True
    return result


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    args = command_parser.parse_args(argv)
    try:
        if args.command == "list":
            data = {
                "harnesses": HARNESS_VERSIONS,
                "task_sets": [
                    {
                        "id": task_set.id,
                        "title": task_set.title,
                        "description": task_set.description,
                        "task_ids": list(task_set.task_ids),
                    }
                    for task_set in TASK_SETS.values()
                ],
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "category": task.category,
                        "prompt": task.prompt,
                        "metadata": task.metadata,
                    }
                    for task in TASKS.values()
                ],
            }
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print("Harnesses: " + ", ".join(f"{h} ({v})" for h, v in HARNESS_VERSIONS.items()))
                print("\nBuilt-in task sets:")
                for task_set in TASK_SETS.values():
                    print(f"  {task_set.id:20} {task_set.title}")
                    print("    " + ", ".join(task_set.task_ids))
                print("\nBuilt-in tasks:")
                for task in TASKS.values():
                    print(f"  {task.id:20} {task.title}")
            return 0
        if args.command == "run":
            report, output = run_benchmark(run_configuration(args))
            print(f"\nJSON: {output / 'results.json'}\nHTML: {output / 'results.html'}")
            if report["status"] == "interrupted":
                return 130
            if report["status"] == "error" or any(
                r["status"] in {"error", "timeout"} for r in report["results"]
            ):
                if report.get("error"):
                    print(f"Error: {report['error']}", file=sys.stderr)
                return 2
            # Correctness failures are benchmark observations, not CLI failures.
            return 0
        if args.command == "build":
            config = RunConfig(
                model="build",
                harnesses=args.harnesses,
                platform=args.platform,
                build_timeout=args.build_timeout,
                harness_versions=versions(args.harness_version),
            ).validate()
            docker = Docker(config)
            docker.available()
            docker.build_images()
            print("Images ready.")
            return 0
        if args.command == "doctor":
            data = doctor_check(args)
            secret = os.environ.get(args.api_key_env)
            text = json.dumps(data, indent=2)
            print(text.replace(secret, "[REDACTED]") if secret else text)
            return 0 if data["ok"] else 2
        if args.command == "report":
            report = json.loads(args.json_file.read_text(encoding="utf-8"))
            if (
                not isinstance(report, dict)
                or report.get("schema_version") != 1
                or not isinstance(report.get("results"), list)
            ):
                raise ValueError("Input must be an agent-bench schema_version 1 report")
            output = args.output or args.json_file.parent
            json_path, html_path = write_reports(report, output)
            print(f"JSON: {json_path}\nHTML: {html_path}")
            return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 2

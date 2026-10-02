"""Publish the reviewed historical run, without Docker, networking, or inference.

This is a source-specific scrub, NOT a generalized privacy guarantee. Review
both generated files before publishing. Requires only stdlib and agent_bench.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from agent_bench import __version__
from agent_bench.report import summarize, write_reports

SOURCE_RUN_ID = "20261002T123149Z-eeb7b0c5"
SOURCE_SHA256 = "8883129e4ed4eea3d2e371efcc3faaebb1500737d38d47b3a7e5dc1f26649035"
PUBLIC_ENDPOINT = "http://model-server.example.invalid:8080/v1"
EXPECTED_SUCCESSES = {"oh-my-pi": 13, "pi": 9, "opencode": 10}


def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def check_privacy(report):
    """Catch obvious residual deployment data; never print suspect raw values."""
    for text in strings(report):
        urls = re.findall(r"https?://[^\s\"'<>]+", text)
        if any(url != PUBLIC_ENDPOINT for url in urls):
            raise ValueError("Unexpected URL remains; review privately before publication")
        if re.search(
            r"/Users/|/home/|[A-Za-z]:\\Users\\|host\.docker\.internal|"
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
            r"(?i:authorization\s*[:=]\s*bearer\s+\S+|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,})",
            text,
        ):
            raise ValueError("Potential private data remains; review privately before publication")
    # 'localhost' and 'password': 'secret' in grading details are literal test
    # inputs in tasks/small_feature.py, not host identity or API credentials.


def sanitize(source, source_sha256):
    if source_sha256 != SOURCE_SHA256 or source.get("run_id") != SOURCE_RUN_ID:
        raise ValueError("This scrub only supports the reviewed original source run/checksum")
    if source.get("status") != "completed" or source["model"]["id"] != "qwen3.8-27b":
        raise ValueError("Expected the completed qwen3.8-27b run")
    summary = summarize(source["results"])
    if summary != source["summary"] or summary["attempts"] != 45 or summary["successes"] != 32:
        raise ValueError("Unexpected measurements or source summary")
    for harness, successes in EXPECTED_SUCCESSES.items():
        metrics = summary["per_harness"][harness]
        if metrics["attempts"] != 15 or metrics["successes"] != successes:
            raise ValueError("Unexpected per-harness measurements")

    report = copy.deepcopy(source)
    for attempt in report["results"]:
        attempt["logs"] = {}
    del report["parameters"]["output"]
    for field in ("NCPU", "MemTotal"):
        del report["environment"]["docker"]["daemon"][field]

    replaced_paths = []

    def replace_endpoints(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}/{key}"
                if key == "base_url":
                    value[key] = PUBLIC_ENDPOINT
                    replaced_paths.append(child)
                else:
                    replace_endpoints(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                replace_endpoints(item, f"{path}/{index}")

    replace_endpoints(report)
    if len(replaced_paths) != 47:
        raise ValueError("Unexpected endpoint layout; review the scrub")
    report["publication"] = {
        "kind": "sanitized measured example",
        "source_run_id": SOURCE_RUN_ID,
        "source_results_json_sha256": source_sha256,
        "source_framework_version": source["framework_version"],
        "renderer_framework_version": __version__,
        "renderer": "agent_bench.report.write_reports from the publishing checkout",
        "removed": [
            {
                "path_pattern": "/results/*/logs",
                "attempts": 45,
                "replacement": {},
                "contents": ["stdout", "stderr", "grader_stdout", "grader_stderr"],
                "reason": "Full logs and agent/tool transcripts withheld for privacy",
            },
            {"path": "/parameters/output", "reason": "Local report-directory metadata omitted"},
            {
                "path": "/environment/docker/daemon/NCPU",
                "reason": "Docker host resource inventory omitted for privacy",
            },
            {
                "path": "/environment/docker/daemon/MemTotal",
                "reason": "Docker host resource inventory omitted for privacy",
            },
        ],
        "replaced": {
            "paths": replaced_paths,
            "replacement": PUBLIC_ENDPOINT,
            "reason": "Private model endpoint replaced with a non-resolving publication placeholder",
        },
        "preserved": [
            "All 45 attempts in original order, including failures",
            "Scores, statuses, grading cases/details/metrics, production patch diffs and source hashes",
            "Timing, usage, termination observations and warnings",
            "Task prompts, fixture/test hashes, schedule and measured run parameters except output/URLs",
            "Recorded environment versions, architectures, local image IDs and creation times",
        ],
        "model_provenance": {
            "recorded_server_alias": "qwen3.8-27b",
            "verification": "Unverified alias; not proof of model identity or parameter count",
            "weights_revision": "unknown; not recorded",
            "quantization": "unknown; not recorded",
            "server_hardware": "unknown; not recorded",
            "server_build_and_launch_flags": "unknown; not recorded",
            "sampling_and_prompt_cache_settings": "unknown; not recorded",
        },
        "limitations": [
            "Five synthetic Python tasks, three repeats per harness; not 45 independent problems",
            "Historical schema-v1 run predates task sets; no measured set selection was added",
            "Framework version alone does not identify an exact source revision or deployment",
            "Container images were amd64 while the recorded host was arm64; timing is deployment-specific",
            "No complete deployment reproducibility claim; local image IDs are not exported images",
            "localhost and password/secret in grading assertions are synthetic task literals",
            "Raw LAN reports remain private and are not included in this example",
            "Source-specific scrub is not a generalized privacy guarantee; manually review before sharing",
        ],
    }
    check_privacy(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Private original results.json")
    parser.add_argument("--output", type=Path, required=True, help="Separate publication directory")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output.is_relative_to(source.parent) or source.is_relative_to(output):
        parser.error("Output must be separate from the private source report directory")
    for name in ("results.json", "results.html"):
        destination = output / name
        if destination.exists() and destination.samefile(source):
            parser.error("Output aliases the private source file")
    print(
        "Warning: this source-specific scrub is not a generalized privacy guarantee. "
        "Manually review JSON and HTML before publication.",
        file=sys.stderr,
    )
    try:
        raw = source.read_bytes()
        report = sanitize(json.loads(raw), hashlib.sha256(raw).hexdigest())
        paths = write_reports(report, output)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Cannot publish example: {exc}\n")
    print("Wrote sanitized results.json and results.html (45 attempts, 32 successes).")
    return paths


if __name__ == "__main__":
    main()

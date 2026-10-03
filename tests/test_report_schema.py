"""Offline report-contract tests; skip validation when jsonschema is unavailable."""

import copy
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from agent_bench.report import summarize, write_reports
from agent_bench.runner import (
    CommandResult,
    ContainerCleanupError,
    RunConfig,
    run_attempt,
    run_benchmark,
)
from agent_bench.tasks import TASKS

try:
    jsonschema = importlib.import_module("jsonschema")
except ModuleNotFoundError as exc:
    if exc.name != "jsonschema":
        raise
    jsonschema = None


def load_schema():
    return json.loads(
        files("agent_bench.schemas").joinpath("results-v1.schema.json").read_text(encoding="utf-8")
    )


def grading(status="passed"):
    return {
        "tests_total": 1,
        "tests_passed": int(status == "passed"),
        "tests_failed": int(status == "failed"),
        "tests_errors": int(status == "error"),
        "score": 100 if status == "passed" else 0,
        "success": status == "passed",
        "cases": [{"name": "hidden.case", "status": status, "detail": ""}],
        "duration_seconds": 0.01,
        "metrics": {"element_reads": 7, "elapsed_seconds": 0.001},
    }


def agent_output():
    return json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "id": "final-message",
                "stopReason": "length",
                "usage": {
                    "input": 10,
                    "output": 5,
                    "cacheRead": 2,
                    "cacheWrite": 0,
                    "reasoning": 3,
                },
            },
        }
    )


def fake_docker(agent=None, grade=None):
    docker = Mock()
    docker.available.return_value = {
        "version": {"Client": {"Version": "test"}, "Server": {"Version": "test"}},
        "daemon": {
            "OSType": "linux",
            "Architecture": "aarch64",
            "OperatingSystem": "test",
            "NCPU": 2,
            "MemTotal": 1024,
        },
    }
    docker.image_metadata.side_effect = lambda tag: {
        "tag": tag,
        "id": "sha256:" + "a" * 64,
        "repo_digests": [],
        "architecture": "arm64",
        "os": "linux",
        "created": "2026-10-01T12:00:00Z",
    }
    agent = agent if agent is not None else CommandResult(0, agent_output(), "", 0.1)
    grade = grade if grade is not None else CommandResult(0, json.dumps(grading()), "", 0.1)
    docker.execute.side_effect = lambda **kwargs: grade if kwargs.get("grade") else agent
    return docker


def produce_report(**overrides):
    """Run the actual report/attempt producers, substituting only Docker I/O."""
    snapshots = []

    def persist(report, output):
        snapshots.append(copy.deepcopy(report))
        write_reports(report, output)

    with (
        tempfile.TemporaryDirectory() as temp,
        patch("agent_bench.runner.Docker", return_value=fake_docker()),
        patch("agent_bench.runner.write_reports", side_effect=persist),
        patch.dict(os.environ, {"AGENT_BENCH_API_KEY": "schema-test-secret"}),
        redirect_stdout(io.StringIO()),
    ):
        options = {
            "model": "test-coder",
            "harnesses": ["pi"],
            "tasks": ["bug-fix"],
            "build": False,
            "output": temp,
            "input_price": 1.0,
            "output_price": 2.0,
        }
        options.update(overrides)
        report, output = run_benchmark(RunConfig(**options))
        saved = json.loads((output / "results.json").read_text(encoding="utf-8"))
        html = (output / "results.html").read_text(encoding="utf-8")
        assert saved == report
        assert "schema-test-secret" not in html
        return report, snapshots


class SchemaResourceTests(unittest.TestCase):
    def test_bundled_resource_is_json_and_uses_draft_2020_12(self):
        schema = load_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"], {"type": "integer", "const": 1})
        self.assertTrue(schema["additionalProperties"])


@unittest.skipIf(jsonschema is None, "optional jsonschema dependency is not installed")
class ReportSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert jsonschema is not None
        cls.schema = load_schema()
        jsonschema.Draft202012Validator.check_schema(cls.schema)
        cls.validator = jsonschema.Draft202012Validator(
            cls.schema, format_checker=jsonschema.FormatChecker()
        )
        cls.report, cls.snapshots = produce_report()

    def assertValid(self, report):
        errors = sorted(self.validator.iter_errors(report), key=lambda error: str(error.path))
        self.assertEqual(
            errors,
            [],
            "\n".join(f"{list(error.path)}: {error.message}" for error in errors),
        )

    def assertInvalidAt(self, path, value):
        report = copy.deepcopy(self.report)
        target = report
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = value
        errors = list(self.validator.iter_errors(report))
        self.assertTrue(errors, f"Accepted invalid value at {path}: {value!r}")
        # anyOf reports at its parent property; its context contains leaf errors.
        pending = list(errors)
        paths = []
        while pending:
            error = pending.pop()
            paths.append(list(error.absolute_path))
            pending.extend(error.context)
        self.assertTrue(
            any(error_path[: len(path)] == list(path) for error_path in paths),
            f"Expected an error at {path}, got {paths}",
        )

    def test_normal_report_and_every_persisted_partial_snapshot(self):
        self.assertEqual(self.report["status"], "completed")
        self.assertEqual(self.report["results"][0]["status"], "success")
        self.assertEqual(self.report["results"][0]["termination"]["reason"], "length")
        self.assertIsNotNone(self.report["results"][0]["usage"]["estimated_cost_usd"])
        self.assertValid(self.report)
        for snapshot in self.snapshots:
            with self.subTest(status=snapshot["status"], results=len(snapshot["results"])):
                self.assertValid(snapshot)
        initial = self.snapshots[0]
        self.assertEqual(initial["status"], "running")
        self.assertIsNone(initial["finished_at"])
        self.assertNotIn("docker", initial["environment"])
        self.assertNotIn("schedule", initial)
        self.assertIsNone(initial["summary"]["success_rate"])

    def test_startup_error_needs_no_docker_images_or_schedule(self):
        docker = fake_docker()
        docker.available.side_effect = RuntimeError("Docker is offline")
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("agent_bench.runner.Docker", return_value=docker),
        ):
            report, output = run_benchmark(RunConfig(model="test-coder", output=temp))
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["results"], [])
            self.assertNotIn("docker", report["environment"])
            self.assertNotIn("base_image", report["environment"])
            self.assertNotIn("harness_images", report["environment"])
            self.assertNotIn("schedule", report)
            self.assertValid(report)
            self.assertValid(json.loads((output / "results.json").read_text(encoding="utf-8")))
            docker.execute.assert_not_called()

    def test_task_sets_overlap_without_overall_double_counting(self):
        report, snapshots = produce_report(
            harnesses=["pi", "opencode"],
            task_sets=["default", "core"],
            tasks=["performance"],
            repeats=2,
        )
        for snapshot in snapshots:
            self.assertValid(snapshot)
        self.assertValid(report)
        self.assertEqual(report["resolved_tasks"], list(TASKS))
        self.assertEqual(report["summary"]["attempts"], 20)
        sets = report["summary"]["per_task_set"]
        self.assertEqual(sets["default"]["attempts"], 20)
        self.assertEqual(sets["core"]["attempts"], 12)
        self.assertEqual(sets["core"]["per_harness"]["pi"]["attempts"], 6)
        self.assertEqual(
            len({(r["harness"], r["task_id"], r["repeat"]) for r in report["results"]}), 20
        )
        for item in [*report["schedule"], *report["results"]]:
            expected = ["default", "core"] if item["task_id"] in list(TASKS)[:3] else ["default"]
            self.assertEqual(item["task_sets"], expected)

    def test_outcome_reason_is_optional_but_constrained_when_present(self):
        self.assertTrue(self.report["results"][0]["outcome_reason"])
        historical = copy.deepcopy(self.report)
        historical["results"][0].pop("outcome_reason")
        self.assertValid(historical)
        for value in (None, "", 123, [], {}):
            with self.subTest(value=value):
                self.assertInvalidAt(["results", 0, "outcome_reason"], value)

    def test_legacy_version_one_without_additive_task_set_and_termination_fields(self):
        # No portable historical results fixture is shipped. This deliberately
        # tests compatibility shape, not a claim of historical live inference.
        report = copy.deepcopy(self.report)
        for key in ("task_set_manifest", "resolved_tasks"):
            report.pop(key)
        report["parameters"].pop("task_sets")
        report["methodology"].pop("task_sets")
        report["methodology"].pop("termination")
        report["summary"].pop("per_task_set")
        for item in [*report["schedule"], *report["results"]]:
            item.pop("task_sets")
        report["results"][0].pop("termination")
        report["results"][0].pop("outcome_reason")
        self.assertValid(report)

    def test_attempt_correctness_failures_and_grader_failures(self):
        scenarios = [
            (CommandResult(0, json.dumps(grading("failed")), "", 0.1), "failed"),
            (CommandResult(0, json.dumps(grading("error")), "", 0.1), "failed"),
            (CommandResult(0, "not json", "", 0.1), "error"),
            (CommandResult(0, "{}", "", 0.1), "error"),
            (CommandResult(1, "", "grader crashed", 0.1), "error"),
            (CommandResult(0, "", "", 0.1, timed_out=True), "timeout"),
            (CommandResult(0, "", "", 0.1, output_limited=True), "error"),
        ]
        for grade, status in scenarios:
            with self.subTest(grade=grade):
                result = run_attempt(
                    fake_docker(grade=grade),
                    RunConfig(model="test-coder"),
                    TASKS["bug-fix"],
                    "pi",
                    1,
                    "agent-id",
                    "grader-id",
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["score"], 0)
                self.assertValidAttempt(result)

    def assertValidAttempt(self, attempt):
        # Validate through the report's results path, using the same bundled refs.
        report = copy.deepcopy(self.report)
        report["results"] = [attempt]
        self.assertValid(report)

    def test_attempt_setup_error_interrupt_cleanup_and_agent_errors(self):
        scenarios = [
            (RuntimeError("startup failure"), "error", False),
            (KeyboardInterrupt(), "interrupted", False),
            (ContainerCleanupError("cleanup failed"), "error", True),
            (CommandResult(2, agent_output(), "failed", 0.1), "error", False),
            (CommandResult(0, agent_output(), "", 0.1, timed_out=True), "timeout", False),
            (CommandResult(0, agent_output(), "", 0.1, output_limited=True), "error", False),
        ]
        for outcome, status, abort in scenarios:
            with self.subTest(outcome=outcome):
                docker = fake_docker(agent=outcome if isinstance(outcome, CommandResult) else None)
                if isinstance(outcome, BaseException):
                    docker.execute.side_effect = outcome
                result = run_attempt(
                    docker,
                    RunConfig(model="test-coder"),
                    TASKS["bug-fix"],
                    "pi",
                    1,
                    "agent-id",
                    "grader-id",
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["score"], 0)
                self.assertEqual(result.get("abort_run", False), abort)
                self.assertValidAttempt(result)
                if isinstance(outcome, CommandResult):
                    self.assertEqual(result["grading"]["score"], 100)
                else:
                    self.assertEqual(result["usage"], {})
                    self.assertEqual(result["patch"], {})
                    self.assertIsNone(result["agent_exit_code"])
        with patch("agent_bench.runner.prepare_harness", side_effect=RuntimeError("no config")):
            result = run_attempt(
                fake_docker(),
                RunConfig(model="test-coder"),
                TASKS["bug-fix"],
                "pi",
                1,
                "agent-id",
                "grader-id",
            )
        self.assertNotIn("harness_configuration", result)
        self.assertNotIn("prompt", result)
        self.assertValidAttempt(result)

    def test_interrupted_and_fatal_cleanup_reports_retain_started_attempt(self):
        for failure, status in (
            (KeyboardInterrupt(), "interrupted"),
            (ContainerCleanupError("cleanup failed"), "error"),
        ):
            docker = fake_docker()
            docker.execute.side_effect = failure
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as temp,
                patch("agent_bench.runner.Docker", return_value=docker),
                redirect_stdout(io.StringIO()),
            ):
                report, output = run_benchmark(
                    RunConfig(
                        model="test-coder", harnesses=["pi"], repeats=2, output=temp, build=False
                    )
                )
                self.assertEqual(report["status"], status)
                self.assertEqual(len(report["results"]), 1)
                self.assertValid(report)
                self.assertValid(json.loads((output / "results.json").read_text(encoding="utf-8")))

    def test_null_unknown_usage_and_known_zero(self):
        unknown = run_attempt(
            fake_docker(agent=CommandResult(0, "", "", 0.1)),
            RunConfig(model="test-coder"),
            TASKS["bug-fix"],
            "pi",
            1,
            "agent-id",
            "grader-id",
        )
        self.assertTrue(all(value is None for value in unknown["usage"].values()))
        self.assertValidAttempt(unknown)
        zero = copy.deepcopy(unknown)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "tool_calls",
            "estimated_cost_usd",
        ):
            zero["usage"][field] = 0
        zero["duration_seconds"] = 0
        zero["agent_exit_code"] = -9  # Return codes are signed, unlike counts.
        self.assertValidAttempt(zero)

    def test_invalid_token_types_and_negative_usage(self):
        for container, fields in (
            (
                "usage",
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "tool_calls",
                ),
            ),
            ("termination", ("output_tokens", "reasoning_tokens")),
        ):
            for field in fields:
                for invalid in (True, False, "12", -1, 1.5, [], {}):
                    with self.subTest(container=container, field=field, value=invalid):
                        self.assertInvalidAt(("results", 0, container, field), invalid)
        for invalid in (-0.01, True, "0"):
            self.assertInvalidAt(("results", 0, "usage", "estimated_cost_usd"), invalid)

    def test_invalid_durations_scores_statuses_and_metadata_types(self):
        cases = [
            (("results", 0, "duration_seconds"), -0.01),
            (("results", 0, "agent_duration_seconds"), -1),
            (("results", 0, "grade_duration_seconds"), -1),
            (("results", 0, "grading", "duration_seconds"), -1),
            (("results", 0, "duration_seconds"), True),
            (("results", 0, "score"), -1),
            (("results", 0, "score"), 101),
            (("results", 0, "score"), True),
            (("results", 0, "grading", "score"), 101),
            (("results", 0, "grading", "success"), 1),
            (("results", 0, "grading", "tests_passed"), True),
            (("results", 0, "grading", "tests_total"), 0),
            (("results", 0, "grading", "cases", 0, "status"), "skipped"),
            (("results", 0, "patch", "lines_added"), -1),
            (("results", 0, "repeat"), 0),
            (("results", 0, "agent_exit_code"), False),
            (("results", 0, "abort_run"), 1),
            (("status",), "finished"),
            (("results", 0, "status"), "completed"),
            (("summary", "mean_score"), 101),
            (("summary", "success_rate"), 1.1),
            (("summary", "completion_rate"), -0.1),
            (("summary", "mean_duration_seconds"), -1),
            (("summary", "attempts"), True),
            (("summary", "usage_samples", "input_tokens"), -1),
            (("summary", "tokens", "input_tokens"), 0.5),
            (("summary", "per_harness", "pi", "mean_score"), -1),
            (("parameters", "timeout"), 0),
            (("parameters", "input_price"), -0.01),
            (("parameters", "build"), 1),
            (("model", "max_tokens"), True),
            (("environment", "credential_present"), 1),
            (("created_at",), "not-a-timestamp"),
            (("results", 0, "source_sha256"), "invalid-hash"),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.assertInvalidAt(path, value)

    def test_rejects_wrong_schema_version_and_missing_core_fields(self):
        for version in (0, 2, "1", True, None):
            with self.subTest(version=version):
                self.assertInvalidAt(("schema_version",), version)
        for key in self.schema["required"]:
            with self.subTest(missing=key):
                report = copy.deepcopy(self.report)
                report.pop(key)
                self.assertTrue(list(self.validator.iter_errors(report)))

    def test_future_additive_fields_and_provider_termination_reasons(self):
        report = copy.deepcopy(self.report)
        attempt = report["results"][0]
        objects = [
            report,
            report["model"],
            report["parameters"],
            report["environment"],
            report["environment"]["host"],
            report["environment"]["docker"],
            report["environment"]["base_image"],
            report["task_manifest"][0],
            report["task_manifest"][0]["metadata"],
            report["schedule"][0],
            attempt,
            attempt["usage"],
            attempt["termination"],
            attempt["patch"],
            attempt["logs"],
            attempt["grading"],
            attempt["grading"]["cases"][0],
            attempt["grading"]["metrics"],
            attempt["harness_configuration"],
            report["summary"],
            report["summary"]["tokens"],
            report["summary"]["usage_samples"],
            report["summary"]["per_harness"]["pi"],
            report["methodology"],
        ]
        for obj in objects:
            obj["future_field"] = {"nested": [None, True, "new observation", 1.5]}
        attempt["termination"]["reason"] = "future-provider-stop-reason"
        self.assertValid(report)

    def test_legacy_version_one_without_later_summary_diagnostics(self):
        report = copy.deepcopy(self.report)
        report.pop("task_set_manifest")
        report.pop("resolved_tasks")
        report["summary"].pop("per_task_set")
        for metrics in [report["summary"], *report["summary"]["per_harness"].values()]:
            for field in (
                "median_duration_seconds",
                "score_stddev",
                "scores_known",
                "durations_known",
                "usage_samples",
            ):
                metrics.pop(field)
        self.assertValid(report)

    def test_summary_null_denominators_and_usage_coverage(self):
        empty = copy.deepcopy(self.snapshots[0])
        self.assertEqual(empty["summary"]["attempts"], 0)
        for field in (
            "success_rate",
            "completion_rate",
            "mean_score",
            "mean_duration_seconds",
            "median_duration_seconds",
            "score_stddev",
        ):
            self.assertIsNone(empty["summary"][field])
        self.assertEqual(empty["summary"]["tokens"], {})
        self.assertEqual(empty["summary"]["usage_samples"], {})
        self.assertValid(empty)

        report = copy.deepcopy(self.report)
        first = report["results"][0]
        second = copy.deepcopy(first)
        second["repeat"] = 2
        second["usage"] = dict.fromkeys(first["usage"])
        report["results"].append(second)
        report["summary"] = summarize(report["results"], report["task_set_manifest"])
        self.assertEqual(report["summary"]["attempts"], 2)
        self.assertEqual(report["summary"]["usage_samples"]["input_tokens"], 1)
        self.assertEqual(report["summary"]["tokens"]["input_tokens"], 10)
        self.assertValid(report)

    def test_additive_task_set_fields_are_open_and_known_fields_are_typed(self):
        report, _ = produce_report(tasks=None, task_sets=["core"])
        report["task_set_manifest"][0]["future_field"] = None
        report["summary"]["per_task_set"]["core"]["future_field"] = True
        self.assertValid(report)
        report["task_set_manifest"][0]["task_ids"] = [True]
        self.assertTrue(list(self.validator.iter_errors(report)))


if __name__ == "__main__":
    unittest.main()

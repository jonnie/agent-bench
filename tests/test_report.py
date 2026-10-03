"""Stdlib-only tests; run with python -B -m unittest discover -s tests."""

from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

# Load the file directly so tests need neither installation nor path configuration.
_SPEC = importlib.util.spec_from_file_location(
    "agent_bench_report", Path(__file__).resolve().parents[1] / "src" / "agent_bench" / "report.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Cannot load report.py")
report_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_module)
render_html = report_module.render_html
summarize = report_module.summarize
write_reports = report_module.write_reports


class Document(HTMLParser):
    """Inspect the parsed document, not just suspicious source substrings."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []
        self.pre_blocks = []
        self._pre = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)
        if tag == "pre":
            self._pre = []

    def handle_endtag(self, tag):
        if tag == "pre" and self._pre is not None:
            self.pre_blocks.append("".join(self._pre))
            self._pre = None

    def handle_data(self, data):
        self.text.append(data)
        if self._pre is not None:
            self._pre.append(data)


def attempt(**overrides):
    result = {
        "harness": "alpha",
        "task_id": "task-1",
        "task_title": "Fix a parser",
        "repeat": 1,
        "status": "success",
        "duration_seconds": 2.0,
        "agent_exit_code": 0,
        "score": 100,
        "grading": {"cases": [{"name": "parses input", "status": "passed", "detail": "OK"}]},
        "usage": None,
        "patch": {"files_changed": 1, "lines_added": 3, "lines_removed": 1, "diff": "+fixed\n-old"},
        "logs": {"stdout": "Done", "stderr": "", "grader_stdout": "1 passed", "grader_stderr": ""},
        "error": None,
    }
    result.update(overrides)
    return result


def report(results=None):
    return {
        "schema_version": 1,
        "run_id": "run-001",
        "created_at": "2026-10-01T12:00:00Z",
        "finished_at": "2026-10-01T12:00:10Z",
        "status": "finished",
        "model": {
            "id": "test-model",
            "base_url": "https://example.test/v1",
            "context_window": 32768,
            "max_tokens": 4096,
            "reasoning": None,
        },
        "parameters": {"temperature": 0, "repeats": 2},
        "environment": {"python": "3.11", "platform": "test"},
        "task_manifest": [{"id": "task-1", "title": "Fix a parser", "category": "Parsing"}],
        "results": [attempt()] if results is None else results,
    }


def task_sets():
    return [
        {
            "id": "recorded-a",
            "title": "Recorded set A",
            "description": "A saved selection, not a live registry lookup.",
            "task_ids": ["task-1", "task-2"],
        },
        {
            "id": "recorded-b",
            "title": "Recorded set B",
            "description": "Overlaps A.",
            "task_ids": ["task-1"],
        },
        {"id": "empty", "title": "Empty set", "description": "No tasks.", "task_ids": []},
    ]


class OutcomeTests(unittest.TestCase):
    def test_failed_grade_explains_counts_case_and_short_assertion_detail(self):
        detail = (
            "Traceback (most recent call last):\n"
            + '  File "/grader/hidden_tests.py", line 42, in test_parser\n' * 30
            + "AssertionError: expected a quoted field, got two fields\n"
        )
        result = attempt(
            status="failed",
            score=50,
            grading={
                "tests_total": 2,
                "tests_passed": 1,
                "tests_failed": 1,
                "tests_errors": 0,
                "score": 50,
                "success": False,
                "cases": [
                    {"name": "hidden.basic", "status": "passed", "detail": ""},
                    {"name": "hidden.quoted_field", "status": "failed", "detail": detail},
                ],
            },
        )
        result.pop("error")
        original = deepcopy(result)
        reason = report_module.describe_outcome(result)
        self.assertIsInstance(reason, str)
        self.assertRegex(reason, r"\b1\s*(?:/|of|out of)\s*2\b")
        self.assertIn("hidden.quoted_field", reason)
        self.assertIn("AssertionError", reason)
        self.assertIn("expected a quoted field, got two fields", reason)
        self.assertNotIn(detail, reason)
        self.assertNotIn("n/a", reason.lower())
        self.assertEqual(result, original)
        self.assertNotIn("error", result)

    def test_candidate_import_error_is_a_grading_failure_not_execution_error(self):
        result = attempt(
            status="failed",
            score=0,
            grading={
                "tests_total": 1,
                "tests_passed": 0,
                "tests_failed": 0,
                "tests_errors": 1,
                "score": 0,
                "success": False,
                "cases": [
                    {
                        "name": "hidden.import_candidate",
                        "status": "error",
                        "detail": "ModuleNotFoundError: No module named 'candidate_dependency'",
                    }
                ],
            },
        )
        result.pop("error")
        reason = report_module.describe_outcome(result)
        self.assertRegex(reason, r"\b0\s*(?:/|of|out of)\s*1\b")
        self.assertIn("hidden.import_candidate", reason)
        self.assertIn("ModuleNotFoundError", reason)
        self.assertIn("candidate_dependency", reason)
        self.assertNotIn("error", result)

    def test_execution_outcomes_explain_observations_and_diagnostic_only_grade(self):
        for status, error in (
            ("timeout", "Agent exceeded its 600 second time limit"),
            ("error", "Model endpoint unavailable"),
            ("interrupted", "Attempt interrupted by user"),
        ):
            with self.subTest(status=status):
                result = attempt(
                    status=status,
                    error=error,
                    score=0,
                    usage={"tool_calls": 3, "input_tokens": None, "output_tokens": None},
                    termination={"reason": "toolUse"},
                    grading={
                        "tests_total": 1,
                        "tests_passed": 1,
                        "score": 100,
                        "success": True,
                        "cases": [{"name": "hidden.case", "status": "passed", "detail": ""}],
                    },
                )
                original = deepcopy(result)
                reason = report_module.describe_outcome(result)
                self.assertIn(error, reason)
                self.assertRegex(reason.lower(), r"tool[_ -]?calls?[^.;\n]*\b3\b")
                self.assertIn("toolUse", reason)
                self.assertIn("100", reason)
                self.assertIn("diagnostic", reason.lower())
                self.assertIn("credit", reason.lower())
                self.assertRegex(reason.lower(), r"(?:no|not|cannot|doesn't|does not).*credit")
                self.assertEqual(result, original)

    def test_legacy_failure_without_grading_is_explicit(self):
        result = attempt(status="failed", grading=None, score=80)
        result.pop("error")
        reason = report_module.describe_outcome(result)
        self.assertIn("no grading", reason.lower())
        self.assertIn("recorded", reason.lower())
        self.assertNotIn("n/a", reason.lower())
        self.assertNotIn("error", result)


class SummarizeTests(unittest.TestCase):
    def test_task_sets_overlap_without_duplicating_overall_metrics(self):
        results = [
            attempt(task_sets=["recorded-a", "recorded-b"], usage={"input_tokens": 10}),
            attempt(
                task_id="task-2",
                status="failed",
                score=0,
                duration_seconds=6,
                task_sets=["recorded-a"],
                usage={"input_tokens": 20},
            ),
            attempt(harness="beta", score=50, duration_seconds=4),
            attempt(task_id="direct-only", score=25, task_sets=[]),
        ]
        manifest = task_sets()
        original = deepcopy((results, manifest))
        summary = summarize(results, task_sets=manifest)
        groups = summary.pop("per_task_set")
        self.assertEqual(summary, summarize(results))
        self.assertEqual(summary["attempts"], 4)
        self.assertEqual(summary["tokens"], {"input_tokens": 30})
        self.assertEqual(groups["recorded-a"], summarize(results[:3]))
        self.assertEqual(groups["recorded-b"], summarize([results[0], results[2]]))
        self.assertEqual(groups["empty"], summarize([]))
        self.assertEqual(groups["recorded-a"]["per_harness"]["alpha"]["mean_score"], 50)
        self.assertEqual((results, manifest), original)

    def test_task_set_groups_use_recorded_task_ids_not_result_memberships(self):
        results = [
            attempt(task_id="archived-task", task_sets=["not-selected"]),
            attempt(task_id="task-1", task_sets=["archived-set"], status="failed", score=0),
        ]
        manifest = [{"id": "archived-set", "task_ids": ["archived-task", "archived-task"]}]
        group = summarize(results, manifest)["per_task_set"]["archived-set"]
        self.assertEqual(group, summarize(results[:1]))
        self.assertEqual(group["attempts"], 1)

    def test_optional_manifest_preserves_legacy_and_empty_sets(self):
        results = [attempt(task_sets=["recorded-a"])]
        legacy = summarize(results)
        self.assertNotIn("per_task_set", legacy)
        self.assertEqual(summarize(results, None), legacy)
        self.assertEqual(summarize(results, []), {**legacy, "per_task_set": {}})
        empty = summarize([], task_sets())
        self.assertEqual(empty["attempts"], 0)
        for group in empty["per_task_set"].values():
            self.assertEqual(group, summarize([]))
        unknown = summarize(
            [attempt(score=None, duration_seconds=None, status="error")], task_sets()
        )["per_task_set"]["recorded-a"]
        self.assertIsNone(unknown["mean_score"])
        self.assertIsNone(unknown["mean_duration_seconds"])
        self.assertEqual(unknown["success_rate"], 0)

    def test_per_harness_and_overall_metrics(self):
        results = [
            attempt(score=100, duration_seconds=2),
            attempt(repeat=2, status="failed", score=50, duration_seconds=4),
            attempt(repeat=3, status="timeout", score=None, duration_seconds=6),
            attempt(repeat=4, status="error", score=None, duration_seconds=None),
            attempt(harness="beta", score=25, duration_seconds=10),
        ]
        original = deepcopy(results)
        summary = summarize(results)
        alpha = summary["per_harness"]["alpha"]
        self.assertEqual(alpha["attempts"], 4)
        self.assertEqual(alpha["successes"], 1)
        self.assertEqual(alpha["success_rate"], 0.25)
        self.assertEqual(alpha["completion_rate"], 0.5)
        self.assertEqual(alpha["mean_score"], 75)
        self.assertEqual(alpha["score_stddev"], 25)
        self.assertEqual(alpha["mean_duration_seconds"], 4)
        self.assertEqual(alpha["median_duration_seconds"], 4)
        self.assertEqual(alpha["scores_known"], 2)
        self.assertEqual(alpha["durations_known"], 3)
        beta = summary["per_harness"]["beta"]
        self.assertEqual(beta["score_stddev"], 0)
        self.assertEqual(beta["success_rate"], 1)
        self.assertEqual(summary["attempts"], 5)
        self.assertEqual(summary["successes"], 2)
        self.assertEqual(summary["success_rate"], 0.4)
        self.assertEqual(summary["mean_score"], 175 / 3)
        self.assertEqual(summary["mean_duration_seconds"], 5.5)
        self.assertEqual(summary["median_duration_seconds"], 5)
        self.assertEqual(results, original)

    def test_empty_and_all_unknown_metrics_are_not_zero(self):
        empty = summarize([])
        self.assertEqual(empty["per_harness"], {})
        self.assertEqual(empty["attempts"], 0)
        self.assertEqual(empty["successes"], 0)
        for field in (
            "success_rate",
            "completion_rate",
            "mean_score",
            "score_stddev",
            "mean_duration_seconds",
            "median_duration_seconds",
        ):
            self.assertIsNone(empty[field], field)
        unknown = summarize([attempt(status="error", score=None, duration_seconds=None)])
        for metrics in (unknown, unknown["per_harness"]["alpha"]):
            self.assertEqual(metrics["success_rate"], 0)
            self.assertIsNone(metrics["mean_score"])
            self.assertIsNone(metrics["score_stddev"])
            self.assertIsNone(metrics["mean_duration_seconds"])
            self.assertIsNone(metrics["median_duration_seconds"])
            self.assertEqual(metrics["tokens"], {})
            self.assertEqual(metrics["usage_samples"], {})
            self.assertNotIn("estimated_cost_usd", metrics)
            self.assertNotIn("tool_calls", metrics)

    def test_partial_usage_has_per_field_coverage_and_known_zero(self):
        results = [
            attempt(
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": None,
                    "cache_write_tokens": 0,
                    "tool_calls": 0,
                    "estimated_cost_usd": 0.1,
                }
            ),
            attempt(
                usage={
                    "input_tokens": None,
                    "output_tokens": 10,
                    "cache_read_tokens": 5,
                    "cache_write_tokens": None,
                    "tool_calls": 2,
                    "estimated_cost_usd": None,
                }
            ),
            attempt(usage=None),
            attempt(harness="beta", usage={"input_tokens": 7, "estimated_cost_usd": 0.2}),
        ]
        summary = summarize(results)
        alpha = summary["per_harness"]["alpha"]
        self.assertEqual(
            alpha["tokens"],
            {
                "input_tokens": 100,
                "output_tokens": 30,
                "cache_read_tokens": 5,
                "cache_write_tokens": 0,
            },
        )
        self.assertEqual(
            alpha["usage_samples"],
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_tokens": 1,
                "cache_write_tokens": 1,
                "tool_calls": 2,
                "estimated_cost_usd": 1,
            },
        )
        self.assertEqual(alpha["tool_calls"], 2)
        self.assertEqual(alpha["estimated_cost_usd"], 0.1)
        self.assertEqual(summary["tokens"]["input_tokens"], 107)
        self.assertEqual(summary["usage_samples"]["input_tokens"], 2)
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.3)
        beta = summary["per_harness"]["beta"]
        self.assertNotIn("output_tokens", beta["tokens"])
        self.assertNotIn("tool_calls", beta)

    def test_zero_scores_durations_and_usage_are_known(self):
        summary = summarize(
            [
                attempt(
                    score=0, duration_seconds=0, usage={"input_tokens": 0, "estimated_cost_usd": 0}
                )
            ]
        )
        self.assertEqual(summary["mean_score"], 0)
        self.assertEqual(summary["mean_duration_seconds"], 0)
        self.assertEqual(summary["tokens"], {"input_tokens": 0})
        self.assertEqual(summary["estimated_cost_usd"], 0)
        self.assertEqual(summary["successes"], 1)  # Status, not score, defines success.

    def test_repeats_are_attempts_not_pass_at_k_or_efficiency_weighted(self):
        summary = summarize(
            [
                attempt(score=100, duration_seconds=1000, usage={"input_tokens": 100000}),
                attempt(
                    repeat=2,
                    status="failed",
                    score=0,
                    duration_seconds=1,
                    usage={"input_tokens": 1},
                ),
            ]
        )
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["mean_score"], 50)
        self.assertNotIn("pass_at_k", summary)

    def test_invalid_numeric_ranges_are_rejected(self):
        for overrides in (
            {"score": -1},
            {"score": 101},
            {"duration_seconds": -1},
            {"usage": {"input_tokens": -1}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                summarize([attempt(**overrides)])

    def test_nonfinite_and_boolean_values_are_not_metrics(self):
        summary = summarize(
            [
                attempt(
                    score=math.nan,
                    duration_seconds=math.inf,
                    usage={"input_tokens": True, "output_tokens": math.nan},
                )
            ]
        )
        self.assertIsNone(summary["mean_score"])
        self.assertIsNone(summary["mean_duration_seconds"])
        self.assertEqual(summary["tokens"], {})


class HtmlTests(unittest.TestCase):
    def test_normal_completion_has_outcome_and_no_na_execution_error(self):
        for status, passed in (("success", 1), ("failed", 0)):
            for error_present in (False, True):
                with self.subTest(status=status, error_present=error_present):
                    result = attempt(
                        status=status,
                        score=100 * passed,
                        agent_exit_code=0,
                        grading={
                            "tests_total": 1,
                            "tests_passed": passed,
                            "success": bool(passed),
                            "score": 100 * passed,
                            "cases": [
                                {
                                    "name": "hidden.case",
                                    "status": "passed" if passed else "failed",
                                    "detail": "" if passed else "AssertionError: expected True",
                                }
                            ],
                        },
                    )
                    if not error_present:
                        result.pop("error")
                    source = report([result])
                    original = deepcopy(source)
                    html = render_html(source)
                    self.assertIn("<h3>Outcome</h3>", html)
                    self.assertIn("<h3>Execution error</h3>", html)
                    self.assertNotIn("<h3>Error</h3>", html)
                    outcome = html.partition("<h3>Outcome</h3>")[2].partition("<h3>")[0]
                    execution = html.partition("<h3>Execution error</h3>")[2].partition("<h3>")[0]
                    self.assertRegex(
                        "".join(Document(outcome).text),
                        rf"\b{passed}\s*(?:/|of|out of)\s*1\b",
                    )
                    self.assertIn("None", execution)
                    self.assertIn("harness execution completed normally", execution.lower())
                    self.assertNotIn("n/a", execution.lower())
                    self.assertEqual(json.loads(Document(html).pre_blocks[-1]), original)
                    self.assertEqual(source, original)

    def test_legacy_html_derives_reasons_without_mutating_full_json(self):
        results = [
            attempt(status="failed", grading=None, score=80),
            attempt(
                status="timeout",
                score=0,
                error="Agent exceeded its 600 second time limit",
                grading=None,
                usage={"tool_calls": 4},
                termination={"reason": "toolUse"},
            ),
        ]
        source = report(results)
        original = deepcopy(source)
        html = render_html(source)
        self.assertEqual(html.count("<h3>Outcome</h3>"), 2)
        outcome_blocks = [part.partition("<h3>")[0] for part in html.split("<h3>Outcome</h3>")[1:]]
        failure_reason = "".join(Document(outcome_blocks[0]).text).lower()
        timeout_reason = "".join(Document(outcome_blocks[1]).text)
        self.assertIn("no grading", failure_reason)
        self.assertIn("recorded", failure_reason)
        self.assertNotIn("n/a", failure_reason)
        self.assertIn(results[1]["error"], timeout_reason)
        self.assertRegex(timeout_reason.lower(), r"tool[_ -]?calls?[^.;\n]*\b4\b")
        self.assertIn("toolUse", timeout_reason)
        self.assertEqual(json.loads(Document(html).pre_blocks[-1]), original)
        self.assertEqual(source, original)
        self.assertTrue(all("outcome_reason" not in result for result in results))

    def test_stored_outcome_is_preferred_escaped_and_roundtrips(self):
        payload = '</pre><script>alert("outcome")</script><img src=x onerror=alert(1)>&\'"'
        source = report([attempt(status="failed", grading=None, outcome_reason=payload)])
        original = deepcopy(source)
        html = render_html(source)
        self.assertIn("<h3>Outcome</h3>", html)
        outcome = html.partition("<h3>Outcome</h3>")[2].partition("<h3>")[0]
        self.assertIn(escape(payload, quote=True), outcome)
        self.assertEqual("".join(Document(outcome).text).strip(), payload)
        document = Document(html)
        self.assertNotIn(payload, html)
        self.assertNotIn("script", document.tags)
        self.assertNotIn("img", document.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in document.attributes))
        self.assertEqual(json.loads(document.pre_blocks[-1]), original)
        self.assertEqual(source, original)

    def test_derived_outcome_and_execution_error_escape_untrusted_strings(self):
        payload = '</pre><script>alert("derived")</script>&\'"'
        source = report(
            [
                attempt(
                    status="error",
                    error=payload,
                    score=0,
                    grading={
                        "tests_total": 1,
                        "tests_passed": 0,
                        "score": 0,
                        "success": False,
                        "cases": [{"name": payload, "status": "error", "detail": payload}],
                    },
                    termination={"reason": payload},
                    usage={"tool_calls": 2},
                )
            ]
        )
        original = deepcopy(source)
        html = render_html(source)
        for heading in ("Outcome", "Execution error"):
            with self.subTest(heading=heading):
                self.assertIn(f"<h3>{heading}</h3>", html)
                section = html.partition(f"<h3>{heading}</h3>")[2].partition("<h3>")[0]
                self.assertIn(escape(payload, quote=True), section)
        document = Document(html)
        self.assertNotIn(payload, html)
        self.assertNotIn("script", document.tags)
        self.assertEqual(json.loads(document.pre_blocks[-1]), original)
        self.assertEqual(source, original)

    def test_task_set_by_harness_comparison_keeps_existing_tables(self):
        source = report(
            [
                attempt(task_sets=["recorded-a", "recorded-b"]),
                attempt(task_id="task-2", status="failed", score=0, duration_seconds=6),
                attempt(harness="beta", score=75, duration_seconds=10),
            ]
        )
        source["task_set_manifest"] = task_sets()
        source["resolved_tasks"] = ["task-1", "task-2"]
        source["summary"] = {"per_task_set": {"stale": {}}}
        html = render_html(source)
        table = html.split("<caption>Task-set-by-harness comparison</caption>", 1)[1]
        body = table.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(body.count("<tr>"), 6)
        self.assertIn(
            "<td>recorded-a</td><td>Recorded set A</td><td>alpha</td><td>2</td>"
            "<td>1</td><td>50.0%</td><td>50.00</td><td>4.00 s</td><td>4.00 s</td>",
            body,
        )
        self.assertIn(
            "<td>recorded-b</td><td>Recorded set B</td><td>beta</td><td>1</td>"
            "<td>1</td><td>100.0%</td><td>75.00</td><td>10.00 s</td><td>10.00 s</td>",
            body,
        )
        self.assertIn(
            "<td>empty</td><td>Empty set</td><td>alpha</td><td>0</td><td>0</td>"
            "<td>n/a</td><td>n/a</td><td>n/a</td><td>n/a</td>",
            body,
        )
        for caption in ("Harness comparison", "Task scores", "Category scores"):
            self.assertIn(f"<caption>{caption}</caption>", html)
        self.assertEqual(json.loads(Document(html).pre_blocks[-1]), source)

    def test_selected_sets_and_membership_are_visible_before_attempts(self):
        source = report([])
        source["status"] = "running"
        source["task_set_manifest"] = task_sets()
        source["resolved_tasks"] = ["task-1", "task-2", "direct-only"]
        html = render_html(source)
        selected = html.split("<caption>Task-set manifest</caption>", 1)[1]
        selected = selected.split("</table>", 1)[0]
        self.assertIn("Recorded set A", selected)
        self.assertIn("A saved selection, not a live registry lookup.", selected)
        membership = html.split("<caption>Resolved task membership</caption>", 1)[1]
        membership = membership.split("</table>", 1)[0]
        self.assertIn(
            "<td>task-1</td><td>[\n  &quot;recorded-a&quot;,\n  &quot;recorded-b&quot;\n]</td>",
            membership,
        )
        self.assertIn("<td>direct-only</td><td>[]</td>", membership)
        self.assertIn("No attempts recorded.", html)
        comparison = html.split("<caption>Task-set-by-harness comparison</caption>", 1)[1]
        comparison = comparison.split("</table>", 1)[0]
        self.assertIn("<td>n/a</td><td>0</td><td>0</td><td>n/a</td>", comparison)
        self.assertEqual(json.loads(Document(html).pre_blocks[-1]), source)

    def test_attempt_membership_uses_recorded_result_field(self):
        source = report([attempt(task_sets=["recorded-result-only"]), attempt(task_sets=[])])
        source["task_set_manifest"] = task_sets()
        html = render_html(source)
        details = html.split('<details class="attempt">')[1:]
        first = details[0].split("<h3>Parameters</h3>", 1)[0]
        second = details[1].split("<h3>Parameters</h3>", 1)[0]
        self.assertIn("<dt>Task sets</dt><dd>[\n  &quot;recorded-result-only&quot;\n]</dd>", first)
        self.assertNotIn("recorded-a", first)
        self.assertIn("<dt>Task sets</dt><dd>[]</dd>", second)

    def test_task_set_metadata_is_escaped_and_full_json_roundtrips(self):
        payload = '</td></pre><script>alert("x")</script><img src=x onerror=alert(1)>&\'"'
        source = report([attempt(task_id=payload, task_sets=[payload])])
        source["task_set_manifest"] = [
            {
                "id": payload,
                "title": payload,
                "description": payload,
                "task_ids": [payload],
            }
        ]
        source["resolved_tasks"] = [payload]
        original = deepcopy(source)
        html = render_html(source)
        document = Document(html)
        self.assertNotIn(payload, html)
        self.assertNotIn("script", document.tags)
        self.assertNotIn("img", document.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in document.attributes))
        for caption in (
            "Task-set manifest",
            "Resolved task membership",
            "Task-set-by-harness comparison",
        ):
            table = html.split(f"<caption>{caption}</caption>", 1)[1].split("</table>", 1)[0]
            self.assertIn(escape(payload, quote=True), table)
        details = html.split('<details class="attempt">', 1)[1].split("<h3>Parameters</h3>", 1)[0]
        self.assertIn(escape(payload, quote=True), details)
        self.assertEqual(json.loads(document.pre_blocks[-1]), source)
        self.assertEqual(source, original)

    def test_legacy_and_direct_task_only_reports(self):
        legacy = render_html(report())
        self.assertNotIn("Selected task sets", legacy)
        self.assertNotIn("Task-set-by-harness comparison", legacy)
        self.assertNotIn("<dt>Task sets</dt>", legacy)
        for results in ([], [attempt(task_sets=[])]):
            with self.subTest(results=results):
                source = report(results)
                source["task_set_manifest"] = []
                source["resolved_tasks"] = ["task-1"]
                html = render_html(source)
                self.assertIn("No task sets selected (direct task selection).", html)
                self.assertIn("<td>task-1</td><td>[]</td>", html)
                self.assertIn("<caption>Task scores</caption>", html)
                self.assertEqual(json.loads(Document(html).pre_blocks[-1]), source)

    def test_standalone_html_and_full_json_roundtrip(self):
        source = report()
        original = deepcopy(source)
        html = render_html(source)
        document = Document(html)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('name="viewport"', html)
        self.assertIn("@media(max-width:700px)", html)
        self.assertIn("details", document.tags)
        self.assertIn("summary", document.tags)
        self.assertNotIn("script", document.tags)
        self.assertNotIn("link", document.tags)
        self.assertNotIn("iframe", document.tags)
        self.assertNotIn("img", document.tags)
        self.assertFalse(any(name in {"src", "href", "action"} for name, _ in document.attributes))
        self.assertNotIn("@import", html)
        self.assertNotIn("url(", html)
        self.assertIn("Content-Security-Policy", html)
        self.assertEqual(document.text.count("MODEL"), 1)
        self.assertEqual(json.loads(document.pre_blocks[-1]), source)
        self.assertIn(
            source["results"][0],
            [
                json.loads(block)
                for block in document.pre_blocks
                if block.startswith('{\n  "harness"')
            ],
        )
        self.assertEqual(source, original)
        for section in (
            "Harness comparison",
            "Category scores",
            "Task scores",
            "Parsing",
            "Run configuration",
            "Attempt details",
            "Parameters",
            "Environment",
            "Grading checks",
            "Agent stdout",
            "Grader stderr",
            "Patch diff",
            "Full report JSON",
            "not efficiency",
            "pass@k",
            "population",
            "Tokens",
            "n/a",
            "file://",
        ):
            self.assertIn(section, html)

    def test_xss_every_dynamic_surface_is_escaped(self):
        payload = '</pre></title><script>alert("x")</script><img src=x onerror=alert(1)>&\'"'
        result = attempt(
            harness=payload,
            task_id=payload,
            task_title=payload,
            repeat=payload,
            status=payload,
            agent_exit_code=payload,
            score=None,
            duration_seconds=None,
            error=payload,
            grading={
                "cases": [{"name": payload, "status": payload, "detail": payload}],
                payload: payload,
            },
            usage={"input_tokens": None, payload: payload},
            termination=dict.fromkeys(
                ("reason", "output_tokens", "reasoning_tokens", "warning"), payload
            ),
            patch={
                "files_changed": payload,
                "lines_added": payload,
                "lines_removed": payload,
                "diff": payload,
            },
            logs={key: payload for key in ("stdout", "stderr", "grader_stdout", "grader_stderr")},
            category=payload,
        )
        source = report([result])
        for key in ("schema_version", "run_id", "created_at", "finished_at", "status"):
            source[key] = payload
        source["model"] = {
            key: payload for key in ("id", "base_url", "context_window", "max_tokens", "reasoning")
        }
        source["parameters"] = {payload: payload}
        source["environment"] = {payload: payload}
        source["task_manifest"] = [{"id": payload, "title": payload, "category": payload}]
        source["summary"] = {payload: payload}
        html = render_html(source)
        document = Document(html)
        self.assertNotIn(payload, html)
        self.assertIn(escape(payload, quote=True), html)
        self.assertNotIn("script", document.tags)
        self.assertNotIn("img", document.tags)
        self.assertNotIn("onerror", [name for name, _ in document.attributes])
        self.assertFalse(any(name.startswith("on") for name, _ in document.attributes))
        self.assertTrue(
            set(document.tags)
            <= {
                "html",
                "head",
                "meta",
                "title",
                "style",
                "body",
                "main",
                "header",
                "p",
                "h1",
                "h2",
                "h3",
                "dl",
                "dt",
                "dd",
                "div",
                "span",
                "strong",
                "section",
                "table",
                "caption",
                "thead",
                "tr",
                "th",
                "tbody",
                "td",
                "details",
                "summary",
                "pre",
                "footer",
                "ul",
                "li",
            }
        )
        self.assertEqual(json.loads(document.pre_blocks[-1]), source)
        self.assertIn(payload, "".join(document.text))
        self.assertIn('class="badge unknown"', html)

    def test_termination_and_warning_are_visible_and_escaped_without_affecting_scores(self):
        payload = '</p><script>alert("x")</script><img src=x onerror=alert(1)>&\'"'
        termination = {
            "reason": payload,
            "output_tokens": 4096,
            "reasoning_tokens": 4000,
            "warning": payload,
        }
        source = report([attempt(termination=termination)])
        original = deepcopy(source)
        html = render_html(source)
        document = Document(html)
        self.assertIn("Final response termination", document.text)
        self.assertIn("Final response output tokens", document.text)
        self.assertIn("Final response reasoning tokens", document.text)
        self.assertIn("4096", document.text)
        self.assertIn("4000", document.text)
        self.assertIn("Warning", document.text)
        self.assertIn("Warning:", document.text)
        self.assertIn(escape(payload, quote=True), html)
        self.assertNotIn(payload, html)
        self.assertNotIn("script", document.tags)
        self.assertNotIn("img", document.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in document.attributes))
        self.assertEqual(json.loads(document.pre_blocks[-1]), source)
        self.assertEqual(source, original)
        self.assertEqual(summarize(source["results"])["mean_score"], 100)
        self.assertEqual(summarize(source["results"])["success_rate"], 1)
        self.assertIn('class="badge success"', html)

    def test_missing_normal_and_unknown_termination_have_no_warning_badge(self):
        for termination in (
            None,
            {},
            {"reason": "stop", "output_tokens": 0, "reasoning_tokens": None, "warning": None},
        ):
            with self.subTest(termination=termination):
                html = render_html(report([attempt(termination=termination)]))
                document = Document(html)
                self.assertIn("Final response termination", document.text)
                self.assertIn("n/a", document.text)
                self.assertNotIn("Warning", document.text)
                self.assertNotIn("Warning:", document.text)
                if termination:
                    self.assertIn("0", document.text)
                    self.assertIn("stop", document.text)

    def test_manifest_titles_and_categories_are_escaped(self):
        payload = '<svg onload="alert(1)">category & title</svg>'
        source = report([attempt(task_title=None)])
        source["task_manifest"] = [
            {"task_id": "task-1", "task_title": payload, "category": payload}
        ]
        html = render_html(source)
        self.assertIn(escape(payload, quote=True), html)
        self.assertNotIn("svg", Document(html).tags)

    def test_category_and_task_scores_are_separate_per_harness(self):
        source = report(
            [
                attempt(score=100),
                attempt(repeat=2, score=0, status="failed"),
                attempt(harness="beta", score=75),
                attempt(task_id="task-2", task_title="Other", score=None),
            ]
        )
        html = render_html(source)
        self.assertIn("Parsing", html)
        self.assertIn("Uncategorized", html)
        self.assertIn("50.00", html)
        self.assertIn("75.00", html)
        self.assertIn("task-2", html)

    def test_task_scores_group_by_id_not_changing_titles(self):
        source = report(
            [
                attempt(task_title="Original title", score=100),
                attempt(task_title="Changed title", repeat=2, score=0, status="failed"),
            ]
        )
        html = render_html(source)
        task_table = html.split("<caption>Task scores</caption>", 1)[1].split("</table>", 1)[0]
        body = task_table.split("<tbody>", 1)[1]
        self.assertEqual(body.count("<tr>"), 1)
        self.assertIn("50.00", body)
        self.assertIn("<td>2</td>", body)

    def test_null_task_metadata_is_not_rendered_as_none(self):
        source = report([attempt(harness=None, task_id=None, task_title=None)])
        source["task_manifest"] = []
        html = render_html(source)
        # An explicit "None" execution-error indicator is not missing metadata.
        before_outcome = html.partition("<h3>Outcome</h3>")[0]
        document = Document(before_outcome)
        self.assertNotIn("None", "".join(document.text))
        self.assertIn("n/a", "".join(document.text))
        self.assertNotIn("<dt>Task ID</dt><dd>None</dd>", html)

    def test_unknown_is_na_but_known_zero_remains_visible(self):
        source = report(
            [
                attempt(
                    score=None,
                    duration_seconds=None,
                    agent_exit_code=None,
                    grading=None,
                    patch=None,
                    logs=None,
                )
            ]
        )
        html = render_html(source)
        self.assertIn("Score n/a / 100", html)
        self.assertIn("<dd>n/a</dd>", html)
        self.assertIn('<span class="muted">n/a</span>', html)
        zero_html = render_html(
            report([attempt(score=0, duration_seconds=0, usage={"input_tokens": 0})])
        )
        self.assertIn("Score 0.00 / 100", zero_html)
        self.assertIn("0.00 s", zero_html)
        self.assertIn("input_tokens: <strong>0</strong>", zero_html)
        self.assertIn("(1/1 attempts)", zero_html)

    def test_empty_and_partial_reports_render(self):
        for source in ({}, report([])):
            html = render_html(source)
            self.assertIn("No attempts recorded.", html)
            self.assertIn("n/a", html)
            self.assertEqual(json.loads(Document(html).pre_blocks[-1]), source)

    def test_summary_is_recomputed_from_results(self):
        source = report()
        source["summary"] = {"mean_score": -100, "per_harness": {}}
        html = render_html(source)
        self.assertIn("100.00", html)
        self.assertIn("alpha", html)
        self.assertEqual(json.loads(Document(html).pre_blocks[-1]), source)


class WriteReportsTests(unittest.TestCase):
    def test_task_set_manifest_drives_saved_summary_and_html(self):
        source = report(
            [
                attempt(task_sets=["recorded-a", "recorded-b"], usage={"input_tokens": 10}),
                attempt(task_id="direct-only", task_sets=[]),
            ]
        )
        source["task_set_manifest"] = task_sets()
        source["resolved_tasks"] = ["task-1", "direct-only"]
        source["summary"] = {"stale": True}
        original = deepcopy(source)
        with tempfile.TemporaryDirectory() as directory:
            json_path, html_path = write_reports(source, Path(directory))
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved,
                {
                    **source,
                    "summary": summarize(source["results"], source["task_set_manifest"]),
                },
            )
            self.assertEqual(saved["summary"]["attempts"], 2)
            self.assertEqual(saved["summary"]["tokens"], {"input_tokens": 10})
            self.assertEqual(saved["summary"]["per_task_set"]["recorded-a"]["attempts"], 1)
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("<caption>Task-set-by-harness comparison</caption>", html)
            self.assertEqual(json.loads(Document(html).pre_blocks[-1]), saved)
        self.assertEqual(source, original)

    def test_empty_and_direct_task_manifests_are_saved_without_observations(self):
        for manifest in (task_sets(), []):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                source = report([])
                source["task_set_manifest"] = manifest
                source["resolved_tasks"] = ["task-1"]
                json_path, html_path = write_reports(source, Path(directory))
                saved = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["summary"], summarize([], manifest))
                self.assertEqual(saved["summary"]["attempts"], 0)
                self.assertEqual(
                    json.loads(Document(html_path.read_text(encoding="utf-8")).pre_blocks[-1]),
                    saved,
                )

    def test_creates_output_preserves_schema_and_replaces_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "reports"
            source = report()
            source["summary"] = {"stale": True}
            original = deepcopy(source)
            paths = write_reports(source, output)
            self.assertEqual(paths, (output / "results.json", output / "results.html"))
            saved = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(saved, {**source, "summary": summarize(source["results"])})
            self.assertEqual(
                json.loads(Document(paths[1].read_text(encoding="utf-8")).pre_blocks[-1]), saved
            )
            self.assertEqual(source, original)
            changed = report([attempt(status="failed", score=0)])
            write_reports(changed, output)
            self.assertEqual(
                json.loads(paths[0].read_text(encoding="utf-8"))["summary"]["mean_score"], 0
            )
            self.assertEqual(set(output.iterdir()), set(paths))

    def test_unicode_and_xss_survive_json_and_stay_escaped_in_html(self):
        with tempfile.TemporaryDirectory() as directory:
            source = report([attempt(task_title='Résumé 🧪 <script>alert("x")</script>')])
            paths = write_reports(source, Path(directory))
            saved = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["results"], source["results"])
            document = Document(paths[1].read_text(encoding="utf-8"))
            self.assertNotIn("script", document.tags)
            self.assertEqual(json.loads(document.pre_blocks[-1]), saved)

    def test_termination_warning_roundtrips_in_json_and_html(self):
        termination = {
            "reason": "length",
            "output_tokens": 4096,
            "reasoning_tokens": None,
            "warning": "Response-token budget exhausted <diagnostic> & not an execution error",
        }
        source = report([attempt(termination=termination)])
        with tempfile.TemporaryDirectory() as temp:
            json_path, html_path = write_reports(source, Path(temp))
            persisted = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["results"][0]["termination"], termination)
            self.assertEqual(persisted["summary"]["mean_score"], 100)
            self.assertEqual(persisted["results"][0]["status"], "success")
            html = html_path.read_text(encoding="utf-8")
            self.assertIn(escape(termination["warning"], quote=True), html)
            self.assertNotIn("diagnostic", Document(html).tags)
            self.assertEqual(json.loads(Document(html).pre_blocks[-1]), persisted)

    def test_both_files_are_staged_before_atomic_replacement(self):
        module = report_module
        real_replace = module.os.replace
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "results.json").write_text("old-json", encoding="utf-8")
            (output / "results.html").write_text("old-html", encoding="utf-8")
            replacements = []

            def replace(source, destination):
                source, destination = Path(source), Path(destination)
                self.assertEqual(source.parent, output)
                self.assertTrue(source.is_file())
                if not replacements:
                    self.assertEqual(len(list(output.glob(".*.tmp"))), 2)
                    self.assertEqual((output / "results.json").read_text(), "old-json")
                    self.assertEqual((output / "results.html").read_text(), "old-html")
                replacements.append(destination.name)
                real_replace(source, destination)

            with patch.object(module.os, "replace", side_effect=replace):
                write_reports(report(), output)
            self.assertEqual(replacements, ["results.json", "results.html"])
            self.assertEqual(list(output.glob(".*.tmp")), [])

    def test_failed_replacement_cleans_staged_files_and_preserves_old_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("results.json", "results.html"):
                (output / name).write_text("old", encoding="utf-8")
            with (
                patch.object(report_module.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                write_reports(report(), output)
            self.assertEqual(list(output.glob(".*.tmp")), [])
            for name in ("results.json", "results.html"):
                self.assertEqual((output / name).read_text(), "old")

    def test_failed_second_staging_does_not_replace_either_file(self):
        module = report_module
        real_temporary = module.tempfile.NamedTemporaryFile
        calls = 0

        def temporary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return real_temporary(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ("results.json", "results.html"):
                (output / name).write_text("old", encoding="utf-8")
            with (
                patch.object(module.tempfile, "NamedTemporaryFile", side_effect=temporary),
                patch.object(module.os, "replace") as replace,
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    write_reports(report(), output)
                replace.assert_not_called()
            self.assertEqual(list(output.glob(".*.tmp")), [])
            for name in ("results.json", "results.html"):
                self.assertEqual((output / name).read_text(), "old")

    def test_serialization_failure_leaves_existing_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            paths = write_reports(report(), output)
            before = [path.read_bytes() for path in paths]
            invalid = report()
            invalid["environment"]["invalid"] = object()
            with self.assertRaises(TypeError):
                write_reports(invalid, output)
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertEqual(set(output.iterdir()), set(paths))

    def test_nonfinite_json_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            invalid = report()
            invalid["parameters"]["invalid"] = math.nan
            with self.assertRaises(ValueError):
                write_reports(invalid, output)
            self.assertFalse(output.exists())

    def test_output_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "file"
            output.write_text("unchanged", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                write_reports(report(), output)
            self.assertEqual(output.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()

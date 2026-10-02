"""Checked-in publication artifacts are measured data, not private transcripts."""

import importlib
import json
import runpy
import unittest
from importlib.resources import files
from pathlib import Path

from agent_bench.report import render_html, summarize

PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "examples/qwen3.8-27b"


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((EXAMPLE / "results.json").read_text(encoding="utf-8"))
        cls.scrubber = runpy.run_path(str(PROJECT / "scripts/make_example.py"))

    def test_measurements_and_failures_are_retained(self):
        self.assertEqual(self.report["status"], "completed")
        self.assertEqual(len(self.report["results"]), 45)
        self.assertEqual(self.report["summary"], summarize(self.report["results"]))
        self.assertEqual(self.report["summary"]["successes"], 32)
        for harness, successes in {"oh-my-pi": 13, "pi": 9, "opencode": 10}.items():
            self.assertEqual(self.report["summary"]["per_harness"][harness]["attempts"], 15)
            self.assertEqual(self.report["summary"]["per_harness"][harness]["successes"], successes)
        self.assertEqual(sum(r["status"] == "failed" for r in self.report["results"]), 13)
        self.assertEqual(sum(bool(r["termination"]["warning"]) for r in self.report["results"]), 2)

    def test_private_transcripts_and_endpoints_are_removed(self):
        self.scrubber["check_privacy"](self.report)
        self.assertTrue(all(r["logs"] == {} for r in self.report["results"]))
        self.assertNotIn("output", self.report["parameters"])
        self.assertNotIn("NCPU", self.report["environment"]["docker"]["daemon"])
        self.assertNotIn("MemTotal", self.report["environment"]["docker"]["daemon"])
        self.assertFalse(self.report["environment"]["credential_present"])
        for value in self.scrubber["strings"](self.report):
            self.assertNotRegex(value, r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
            self.assertNotIn("/Users/", value)

    def test_html_is_rendered_from_the_sanitized_json(self):
        html = (EXAMPLE / "results.html").read_text(encoding="utf-8")
        self.assertEqual(html, render_html(self.report))
        self.assertNotRegex(html, r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        self.assertNotIn("/Users/", html)
        self.assertNotIn("<script", html.lower())

    def test_historical_provenance_does_not_invent_task_sets(self):
        publication = self.report["publication"]
        self.assertEqual(publication["kind"], "sanitized measured example")
        self.assertEqual(publication["source_run_id"], self.report["run_id"])
        self.assertEqual(publication["source_results_json_sha256"], self.scrubber["SOURCE_SHA256"])
        self.assertNotIn("task_set_manifest", self.report)
        self.assertNotIn("resolved_tasks", self.report)
        self.assertIn("unknown", publication["model_provenance"]["quantization"])

    def test_scrubber_rejects_unreviewed_source(self):
        with self.assertRaisesRegex(ValueError, "reviewed original source"):
            self.scrubber["sanitize"](self.report, "0" * 64)

    def test_published_example_matches_bundled_schema(self):
        try:
            jsonschema = importlib.import_module("jsonschema")
        except ModuleNotFoundError as exc:
            if exc.name != "jsonschema":
                raise
            self.skipTest("optional jsonschema dependency is not installed")
        schema = json.loads(
            files("agent_bench.schemas")
            .joinpath("results-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        validator.validate(self.report)


if __name__ == "__main__":
    unittest.main()

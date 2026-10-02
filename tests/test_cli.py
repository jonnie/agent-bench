import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bench.cli import main, parser, run_configuration
from agent_bench.harnesses import HARNESS_VERSIONS


class CLITests(unittest.TestCase):
    def test_defaults_and_url_alias(self):
        args = parser().parse_args(
            ["run", "--model", "qwen/coder", "--llama-url", "http://192.168.1.55:8080"]
        )
        config = run_configuration(args)
        self.assertEqual(config.base_url, "http://192.168.1.55:8080/v1")
        self.assertEqual(config.harnesses, list(HARNESS_VERSIONS))
        self.assertEqual(config.repeats, 1)
        self.assertTrue(config.build)
        self.assertEqual(config.tool_profile, "native")
        self.assertEqual(config.api_profile, "llama-cpp")
        self.assertEqual([s.id for s in config.selected_task_sets()], ["default"])
        self.assertEqual(len(config.selected_tasks()), 5)

    def test_api_profile_can_be_overridden(self):
        config = run_configuration(
            parser().parse_args(["run", "--model", "coder", "--api-profile", "openai-compatible"])
        )
        self.assertEqual(config.api_profile, "openai-compatible")

    def test_config_cli_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "model": "coder",
                        "base_url": "http://server:8080",
                        "repeats": 3,
                        "harnesses": ["pi"],
                        "build": False,
                        "harness_versions": {"pi": "0.99.2"},
                    }
                )
            )
            args = parser().parse_args(
                ["run", "--config", str(path), "--repeats", "2", "--harness-version", "pi=0.99.1"]
            )
            config = run_configuration(args)
            self.assertEqual(config.model, "coder")
            self.assertEqual(config.repeats, 2)
            self.assertFalse(config.build)
            self.assertEqual(config.harnesses, ["pi"])
            self.assertEqual(config.harness_versions["pi"], "0.99.1")

    def test_reject_unknown_config_or_missing_model(self):
        with self.assertRaises(ValueError):
            run_configuration(parser().parse_args(["run"]))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text('{"model":"coder","api_key":"never store keys"}')
            with self.assertRaisesRegex(ValueError, "Unknown config"):
                run_configuration(parser().parse_args(["run", "--config", str(path)]))

    def test_no_build_and_restrictions(self):
        config = run_configuration(
            parser().parse_args(
                [
                    "run",
                    "--model",
                    "coder",
                    "--no-build",
                    "--harnesses",
                    "pi",
                    "--tasks",
                    "bug-fix",
                    "--reasoning",
                    "--thinking",
                    "high",
                ]
            )
        )
        self.assertFalse(config.build)
        self.assertTrue(config.reasoning)
        self.assertEqual(config.thinking, "high")
        self.assertEqual([t.id for t in config.selected_tasks()], ["bug-fix"])
        self.assertEqual(config.selected_task_sets(), [])
        with self.assertRaises(ValueError):
            run_configuration(
                parser().parse_args(["run", "--model", "coder", "--thinking", "high"])
            )

    def test_list_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["list", "--json"])
        data = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(len(data["tasks"]), 5)
        self.assertEqual(len(data["harnesses"]), 3)
        sets = {s["id"]: s for s in data["task_sets"]}
        self.assertEqual(set(sets), {"default", "core", "robustness"})
        self.assertEqual(sets["core"]["task_ids"], ["bug-fix", "small-feature", "system-refactor"])

    def test_task_set_selection_and_explicit_task_union(self):
        config = run_configuration(
            parser().parse_args(
                [
                    "run",
                    "--model",
                    "coder",
                    "--task-sets",
                    "core",
                    "robustness",
                    "--tasks",
                    "bug-fix",
                ]
            )
        )
        self.assertEqual(config.task_sets, ["core", "robustness"])
        self.assertEqual(config.tasks, ["bug-fix"])
        self.assertEqual(
            [t.id for t in config.selected_tasks()],
            [
                "bug-fix",
                "small-feature",
                "system-refactor",
                "security-hardening",
                "performance",
            ],
        )

    def test_cli_selectors_replace_entire_config_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "model": "coder",
                        "tasks": ["bug-fix"],
                        "task_sets": ["core"],
                        "repeats": 3,
                    }
                )
            )
            cases = (
                (["--tasks", "performance"], ["performance"], []),
                (
                    ["--task-sets", "robustness"],
                    ["security-hardening", "performance"],
                    ["robustness"],
                ),
                (
                    ["--task-sets", "robustness", "--tasks", "bug-fix"],
                    ["security-hardening", "performance", "bug-fix"],
                    ["robustness"],
                ),
                ([], ["bug-fix", "small-feature", "system-refactor"], ["core"]),
            )
            for flags, tasks, sets in cases:
                with self.subTest(flags=flags):
                    config = run_configuration(
                        parser().parse_args(
                            [
                                "run",
                                "--config",
                                str(path),
                                *flags,
                            ]
                        )
                    )
                    self.assertEqual([t.id for t in config.selected_tasks()], tasks)
                    self.assertEqual([s.id for s in config.selected_task_sets()], sets)
                    self.assertEqual(config.repeats, 3)

    def test_invalid_config_task_set_selection_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for selection in (
                {"task_sets": ["unknown"]},
                {"task_sets": []},
                {"task_sets": ["core", "core"]},
                {"task_sets": "core"},
                {"task_sets": [42]},
                {"tasks": ["unknown"]},
            ):
                path.write_text(json.dumps({"model": "coder", **selection}))
                with self.subTest(selection=selection), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["run", "--config", str(path)]), 2)

    def test_list_text_includes_task_sets_and_members(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("Built-in task sets:", output.getvalue())
        self.assertIn("core", output.getvalue())
        self.assertIn("security-hardening, performance", output.getvalue())

    def test_invalid_inputs_return_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run"]), 2)
            self.assertEqual(main(["run", "--model", "coder", "--base-url", "file:///tmp"]), 2)

    def test_correctness_failure_is_valid_run_but_errors_nonzero(self):
        for report, expected in (
            ({"status": "completed", "results": [{"status": "failed"}]}, 0),
            ({"status": "completed", "results": [{"status": "error"}]}, 2),
            ({"status": "interrupted", "results": []}, 130),
        ):
            with (
                self.subTest(report=report),
                patch("agent_bench.cli.run_benchmark", return_value=(report, Path("results/test"))),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["run", "--model", "coder"]), expected)


if __name__ == "__main__":
    unittest.main()

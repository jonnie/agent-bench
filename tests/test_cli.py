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
        self.assertEqual(config.timeout, 600)
        self.assertEqual(config.max_log_bytes, 2_000_000)
        self.assertEqual(config.grade_timeout, 60)
        self.assertEqual(config.build_timeout, 900)
        self.assertEqual(config.max_tokens, 4096)
        self.assertTrue(config.build)
        self.assertEqual(config.tool_profile, "native")
        self.assertEqual(config.api_profile, "llama-cpp")
        self.assertEqual([s.id for s in config.selected_task_sets()], ["default"])
        self.assertEqual(len(config.selected_tasks()), 5)

    def test_unlimited_cli_limits_independently_and_together(self):
        cases = (
            (["--timeout", "unlimited"], None, 2_000_000),
            (["--max-log-bytes", "unlimited"], 600, None),
            (["--timeout", "UNLIMITED", "--max-log-bytes", "UnLiMiTeD"], None, None),
            (["--timeout", "UnLiMiTeD"], None, 2_000_000),
            (["--max-log-bytes", "UNLIMITED"], 600, None),
            (["--timeout", "2.5", "--max-log-bytes", "1234"], 2.5, 1234),
        )
        for flags, timeout, max_log_bytes in cases:
            with self.subTest(flags=flags):
                config = run_configuration(parser().parse_args(["run", "--model", "coder", *flags]))
                self.assertEqual(config.timeout, timeout)
                self.assertEqual(config.max_log_bytes, max_log_bytes)
                self.assertEqual(config.grade_timeout, 60)
                self.assertEqual(config.build_timeout, 900)
                self.assertEqual(config.max_tokens, 4096)

    def test_null_json_limits_independently_and_together(self):
        cases = (
            ({}, 600, 2_000_000),
            ({"timeout": None}, None, 2_000_000),
            ({"max_log_bytes": None}, 600, None),
            ({"timeout": None, "max_log_bytes": None}, None, None),
            ({"timeout": 2.5, "max_log_bytes": 1234}, 2.5, 1234),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for limits, timeout, max_log_bytes in cases:
                with self.subTest(limits=limits):
                    path.write_text(json.dumps({"model": "coder", **limits}))
                    config = run_configuration(parser().parse_args(["run", "--config", str(path)]))
                    self.assertEqual(config.timeout, timeout)
                    self.assertEqual(config.max_log_bytes, max_log_bytes)

    def test_cli_overrides_finite_and_null_json_limits_in_both_directions(self):
        cases = (
            ({"timeout": 1800, "max_log_bytes": 1234}, ["--timeout", "unlimited"], None, 1234),
            (
                {"timeout": 1800, "max_log_bytes": 1234},
                ["--max-log-bytes", "unlimited"],
                1800,
                None,
            ),
            (
                {"timeout": 1800, "max_log_bytes": 1234},
                ["--timeout", "unlimited", "--max-log-bytes", "unlimited"],
                None,
                None,
            ),
            ({"timeout": None, "max_log_bytes": None}, ["--timeout", "1800"], 1800, None),
            ({"timeout": None, "max_log_bytes": None}, ["--max-log-bytes", "1234"], None, 1234),
            (
                {"timeout": None, "max_log_bytes": None},
                ["--timeout", "1800", "--max-log-bytes", "1234"],
                1800,
                1234,
            ),
            (
                {"timeout": None, "max_log_bytes": 1234},
                ["--timeout", "2.5", "--max-log-bytes", "unlimited"],
                2.5,
                None,
            ),
            (
                {"timeout": 1800, "max_log_bytes": None},
                ["--timeout", "unlimited", "--max-log-bytes", "5678"],
                None,
                5678,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for limits, flags, timeout, max_log_bytes in cases:
                with self.subTest(limits=limits, flags=flags):
                    path.write_text(json.dumps({"model": "coder", **limits}))
                    config = run_configuration(
                        parser().parse_args(["run", "--config", str(path), *flags])
                    )
                    self.assertEqual(config.timeout, timeout)
                    self.assertEqual(config.max_log_bytes, max_log_bytes)

    def test_invalid_cli_limits_are_rejected(self):
        invalid_values = (
            "NaN",
            "inf",
            "-inf",
            "Infinity",
            "0",
            "-1",
            "true",
            "false",
            "null",
            "bad",
        )
        for option in (
            "--timeout",
            "--max-log-bytes",
            "--grade-timeout",
            "--build-timeout",
            "--max-tokens",
        ):
            values = invalid_values + (
                ("1.5",) if option in ("--max-log-bytes", "--max-tokens") else ()
            )
            for value in values:
                with (
                    self.subTest(option=option, value=value),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises((SystemExit, ValueError)) as raised,
                ):
                    run_configuration(
                        parser().parse_args(["run", "--model", "coder", f"{option}={value}"])
                    )
                if isinstance(raised.exception, SystemExit):
                    self.assertEqual(raised.exception.code, 2)

    def test_other_timeouts_and_max_tokens_do_not_accept_unlimited(self):
        for command, option in (
            ("run", "--grade-timeout"),
            ("run", "--build-timeout"),
            ("run", "--max-tokens"),
            ("doctor", "--timeout"),
        ):
            for value in ("unlimited", "UNLIMITED"):
                with (
                    self.subTest(command=command, option=option, value=value),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    parser().parse_args([command, "--model", "coder", option, value])
                self.assertEqual(raised.exception.code, 2)
        for value in ("NaN", "inf", "-inf", "0", "-1", "true", "false"):
            with (
                self.subTest(command="doctor", value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                parser().parse_args(["doctor", f"--timeout={value}"])
            self.assertEqual(raised.exception.code, 2)
        config = run_configuration(
            parser().parse_args(
                [
                    "run",
                    "--model",
                    "coder",
                    "--grade-timeout",
                    "1.5",
                    "--build-timeout",
                    "30",
                    "--max-tokens",
                    "64",
                ]
            )
        )
        self.assertEqual(config.grade_timeout, 1.5)
        self.assertEqual(config.build_timeout, 30)
        self.assertEqual(config.max_tokens, 64)
        self.assertEqual(parser().parse_args(["doctor", "--timeout", "2.5"]).timeout, 2.5)

    def test_invalid_json_limits_are_rejected(self):
        invalid_values = (
            True,
            False,
            0,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            "bad",
            [],
            {},
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for field in (
                "timeout",
                "max_log_bytes",
                "grade_timeout",
                "build_timeout",
                "max_tokens",
            ):
                values = invalid_values
                if field in ("max_log_bytes", "max_tokens"):
                    values += (1.5,)
                if field not in ("timeout", "max_log_bytes"):
                    values += (None, "unlimited")
                for value in values:
                    with self.subTest(field=field, value=value):
                        path.write_text(json.dumps({"model": "coder", field: value}))
                        with self.assertRaises(ValueError):
                            run_configuration(parser().parse_args(["run", "--config", str(path)]))

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

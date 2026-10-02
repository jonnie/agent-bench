import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_bench.runner import (
    CommandResult,
    ContainerCleanupError,
    Docker,
    RunConfig,
    normalize_base_url,
    patch_metrics,
    run_attempt,
    run_benchmark,
    run_command,
    snapshot_sources,
    validate_grade,
)
from agent_bench.tasks import TASKS


def grade(success=True):
    return {
        "tests_total": 1,
        "tests_passed": int(success),
        "tests_failed": int(not success),
        "tests_errors": 0,
        "score": 100 if success else 0,
        "success": success,
        "cases": [
            {"name": "hidden.case", "status": "passed" if success else "failed", "detail": ""}
        ],
    }


class FakeDocker(Docker):
    def __init__(self, agent=None, grading=None):
        super().__init__(RunConfig(model="test"))
        self.agent = agent or CommandResult(0, "", "", 0.1)
        self.grading = grading or CommandResult(0, json.dumps(grade()), "", 0.1)
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.grading if kwargs.get("grade") else self.agent


class RunnerTests(unittest.TestCase):
    def test_url_normalization(self):
        for source in (
            "http://192.168.1.2:8080",
            "http://192.168.1.2:8080/",
            "http://192.168.1.2:8080/v1/",
        ):
            self.assertEqual(normalize_base_url(source), "http://192.168.1.2:8080/v1")
        self.assertEqual(normalize_base_url("https://server/proxy/v1"), "https://server/proxy/v1")
        for bad in (
            "file:///tmp/a",
            "http://user:key@host",
            "http://host?key=secret",
            "http://host/v1/models",
            "http://host/v1/chat/completions",
            "http://host:abc",
            "http://host/#foo",
            "http://host bad",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_limits_and_duplicates(self):
        cases: tuple[dict[str, Any], ...] = (
            {"cpus": 0},
            {"cpus": float("nan")},
            {"timeout": -1},
            {"repeats": 0},
            {"tasks": []},
            {"tasks": ["bug-fix", "bug-fix"]},
            {"harnesses": ["pi", "pi"]},
            {"max_tokens": 40000},
            {"memory": "0g"},
            {"network": "none"},
            {"platform": "windows/amd64"},
            {"seed": []},
            {"build": "false"},
            {"network": 42},
            {"cpus": "two"},
            {"tasks": "bug-fix"},
            {"task_sets": []},
            {"task_sets": "core"},
            {"task_sets": [False]},
            {"task_sets": ["unknown"]},
            {"task_sets": ["core", "core"]},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunConfig(model="coder", **kwargs).validate()

    def test_task_selection_defaults_union_and_validation_are_idempotent(self):
        cases: tuple[tuple[dict[str, Any], list[str], list[str]], ...] = (
            ({}, list(TASKS), ["default"]),
            ({"task_sets": ["core"]}, list(TASKS)[:3], ["core"]),
            ({"tasks": ["performance"]}, ["performance"], []),
            (
                {"task_sets": ["robustness", "core"], "tasks": ["bug-fix"]},
                [
                    "security-hardening",
                    "performance",
                    "bug-fix",
                    "small-feature",
                    "system-refactor",
                ],
                ["robustness", "core"],
            ),
        )
        for selection, tasks, sets in cases:
            with self.subTest(selection=selection):
                config = RunConfig(model="coder", **selection)
                for _ in range(2):
                    self.assertIs(config.validate(), config)
                    self.assertEqual([t.id for t in config.selected_tasks()], tasks)
                    self.assertEqual([s.id for s in config.selected_task_sets()], sets)
                self.assertEqual(config.tasks, selection.get("tasks"))
                self.assertEqual(config.task_sets, selection.get("task_sets"))

    def test_snapshot_only_python_no_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            (work / "solution").mkdir(parents=True)
            (work / "solution/main.py").write_text("raise RuntimeError('never import this')\n")
            (work / "solution/data.json").write_text("{}")
            (work / "tests").mkdir()
            (work / "tests/test.py").write_text("raise SystemExit()")
            result = snapshot_sources(work, root / "snapshot", 1000)
            self.assertEqual(list(result), ["solution/main.py"])
            self.assertTrue((root / "snapshot/solution/main.py").is_file())

    def test_snapshot_refuses_symlinks_size_and_nonutf8(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            (work / "solution").mkdir(parents=True)
            source = work / "solution/bad.py"
            source.symlink_to(root / "outside.py")
            with self.assertRaises(ValueError):
                snapshot_sources(work, root / "out", 100)
            source.unlink()
            source.write_bytes(b"x" * 101)
            with self.assertRaises(ValueError):
                snapshot_sources(work, root / "out", 100)
            source.write_bytes(b"\xff")
            with self.assertRaises(ValueError):
                snapshot_sources(work, root / "out", 100)
            source.unlink()
            (work / "solution/link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                snapshot_sources(work, root / "out", 100)

    def test_patch_counts_add_delete_change_and_empty_file(self):
        before = {
            "solution/a.py": "one\ntwo\n",
            "solution/gone.py": "deleted\n",
            "tests/test.py": "ignored",
        }
        after = {
            "solution/a.py": "one\nthree\n",
            "solution/new.py": "new\n",
            "solution/empty.py": "",
        }
        result = patch_metrics(before, after)
        self.assertEqual(result["files_changed"], 4)
        self.assertEqual(result["lines_added"], 2)
        self.assertEqual(result["lines_removed"], 2)
        self.assertNotIn("test.py", result["diff"])

    def test_grade_validation(self):
        self.assertEqual(validate_grade(grade()), grade())
        for key, value in (
            ("score", 9),
            ("tests_passed", True),
            ("tests_total", 0),
            ("success", False),
            ("cases", []),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_grade({**grade(), key: value})

    def test_attempt_mount_isolation_and_redaction(self):
        docker = FakeDocker(agent=CommandResult(0, "API key SECRETVALUE", "", 0.1))
        with patch.dict(os.environ, {"AGENT_BENCH_API_KEY": "SECRETVALUE"}):
            result = run_attempt(
                docker, RunConfig(model="coder"), TASKS["bug-fix"], "pi", 1, "agent-id", "base-id"
            )
        self.assertEqual(result["status"], "success")
        self.assertNotIn("SECRETVALUE", json.dumps(result))
        agent, grader = docker.calls
        self.assertEqual(agent["image"], "agent-id")
        self.assertEqual(grader["image"], "base-id")
        self.assertFalse(agent.get("grade", False))
        self.assertTrue(grader["grade"])
        self.assertNotIn("environment", grader)
        self.assertTrue(all(readonly for _, _, readonly in grader["mounts"]))
        self.assertEqual(agent["mounts"][0][1], "/workspace")
        self.assertNotIn("/grader", [target for _, target, _ in agent["mounts"]])
        self.assertTrue(all(readonly for _, _, readonly in agent["mounts"][1:]))

    def test_agent_errors_are_zero_even_when_patch_passes(self):
        for agent in (
            CommandResult(-9, "", "", 0.1, timed_out=True),
            CommandResult(-9, "", "", 0.1, output_limited=True),
            CommandResult(1, "", "oops", 0.1),
            CommandResult(0, '{"type":"error","message":"model offline"}', "", 0.1),
        ):
            with self.subTest(agent=agent):
                result = run_attempt(
                    FakeDocker(agent=agent),
                    RunConfig(model="coder"),
                    TASKS["bug-fix"],
                    "pi",
                    1,
                    "a",
                    "b",
                )
                self.assertIn(result["status"], {"timeout", "error"})
                self.assertEqual(result["score"], 0)
                self.assertTrue(result["grading"]["success"])

    def test_length_warning_preserves_correctness_status_and_configuration(self):
        partial = {
            **grade(False),
            "tests_total": 2,
            "tests_passed": 1,
            "score": 50,
            "cases": grade()["cases"] + grade(False)["cases"],
        }
        for harness in ("pi", "oh-my-pi", "opencode"):
            if harness == "opencode":
                event = {
                    "type": "step_finish",
                    "part": {"reason": "length", "tokens": {"output": 4096, "reasoning": 4000}},
                }
            else:
                event = {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "length",
                        "usage": {"output": 4096, "reasoning": 4000},
                    },
                }
            for grading in (grade(), grade(False), partial):
                for api_profile in ("llama-cpp", "openai-compatible"):
                    with self.subTest(harness=harness, grading=grading, api_profile=api_profile):
                        docker = FakeDocker(
                            agent=CommandResult(0, json.dumps(event), "", 0.1),
                            grading=CommandResult(0, json.dumps(grading), "", 0.1),
                        )
                        result = run_attempt(
                            docker,
                            RunConfig(model="coder", api_profile=api_profile),
                            TASKS["bug-fix"],
                            harness,
                            1,
                            "a",
                            "b",
                        )
                        self.assertEqual(
                            result["status"], "success" if grading["success"] else "failed"
                        )
                        self.assertEqual(result["score"], grading["score"])
                        self.assertEqual(result["grading"], grading)
                        self.assertNotIn("error", result)
                        self.assertEqual(result["termination"]["reason"], "length")
                        self.assertEqual(result["termination"]["output_tokens"], 4096)
                        self.assertEqual(result["termination"]["reasoning_tokens"], 4000)
                        self.assertIsNotNone(result["termination"]["warning"])
                        self.assertEqual(
                            result["harness_configuration"]["api_profile"], api_profile
                        )
                        self.assertEqual(result["harness_configuration"]["max_tokens"], 4096)
                        self.assertEqual(len(docker.calls), 2)

    def test_length_does_not_hide_execution_errors_or_timeouts(self):
        stream = json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "length", "usage": {"output": 4096}},
            }
        )
        for agent, expected in (
            (CommandResult(1, stream, "failed", 0.1), "error"),
            (CommandResult(-9, stream, "", 0.1, timed_out=True), "timeout"),
            (CommandResult(-9, stream, "", 0.1, output_limited=True), "error"),
            (CommandResult(0, stream + '\n{"type":"error","message":"offline"}', "", 0.1), "error"),
        ):
            with self.subTest(agent=agent):
                result = run_attempt(
                    FakeDocker(agent=agent),
                    RunConfig(model="coder"),
                    TASKS["bug-fix"],
                    "pi",
                    1,
                    "a",
                    "b",
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["score"], 0)
                self.assertTrue(result["grading"]["success"])
                self.assertIsNotNone(result["termination"]["warning"])

    def test_recovered_length_attempt_has_no_warning(self):
        stream = "\n".join(
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": reason,
                        "usage": {"output": count},
                    },
                }
            )
            for reason, count in (("length", 4096), ("stop", 10))
        )
        result = run_attempt(
            FakeDocker(agent=CommandResult(0, stream, "", 0.1)),
            RunConfig(model="coder"),
            TASKS["bug-fix"],
            "pi",
            1,
            "a",
            "b",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["usage"]["output_tokens"], 4106)
        self.assertEqual(result["termination"]["output_tokens"], 10)
        self.assertIsNone(result["termination"]["warning"])

    def test_invalid_and_timed_out_grade(self):
        for grading in (
            CommandResult(0, "not json", "", 0.1),
            CommandResult(0, "", "", 0.1, timed_out=True),
        ):
            with self.subTest(grading=grading):
                result = run_attempt(
                    FakeDocker(grading=grading),
                    RunConfig(model="coder"),
                    TASKS["bug-fix"],
                    "pi",
                    1,
                    "a",
                    "b",
                )
                self.assertIn(result["status"], {"timeout", "error"})
                self.assertEqual(result["score"], 0)

    def test_docker_grade_flags_and_cleanup(self):
        config = RunConfig(model="coder")
        calls = []

        def command(argv, *args, **kwargs):
            calls.append((argv, kwargs))
            return CommandResult(0, "{}", "", 0.1)

        with patch("agent_bench.runner.run_command", side_effect=command):
            Docker(config).execute(
                image="sha256:test", mounts=[], command=["python", "-I"], timeout=2, grade=True
            )
        argv = calls[0][0]
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--read-only", argv)
        self.assertIn("--cap-drop", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertNotIn("--env", argv)
        self.assertEqual(calls[-1][0][:3], ["docker", "rm", "--force"])

    def test_linux_agent_uses_host_uid_and_private_writable_home(self):
        calls = []
        homes = []
        environment = {"BENCH_API_KEY": "environment-only-secret", "HOME": "/home/bench"}
        with tempfile.TemporaryDirectory() as temp:
            config_file = Path(temp) / "models.json"
            config_file.write_text("{}", encoding="utf-8")
            mounts = [(config_file, "/home/bench/.pi/agent/models.json", True)]

            def command(argv, *args, **kwargs):
                calls.append((argv, kwargs))
                if argv[:2] == ["docker", "run"]:
                    self.assertEqual(argv[argv.index("--user") + 1], "1001:1002")
                    self.assertNotIn("environment-only-secret", argv)
                    self.assertEqual(kwargs["env"]["BENCH_API_KEY"], environment["BENCH_API_KEY"])
                    specs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
                    home_spec = next(
                        spec
                        for spec in specs
                        if "target=/home/bench," in spec or spec.endswith("target=/home/bench")
                    )
                    self.assertNotIn("readonly", home_spec)
                    home = Path(home_spec.split("source=", 1)[1].split(",", 1)[0])
                    homes.append(home)
                    self.assertNotEqual(home.resolve(), Path.home().resolve())
                    self.assertEqual(home.stat().st_mode & 0o777, 0o700)
                    self.assertTrue((home / ".pi/agent/models.json").is_file())
                    self.assertTrue(
                        any(
                            spec.endswith("target=/home/bench/.pi/agent/models.json,readonly")
                            for spec in specs
                        )
                    )
                    cache = home / ".cache/private"
                    cache.mkdir(parents=True, mode=0o700)
                    (cache / "artifact").write_text("cache", encoding="utf-8")
                elif argv[:3] == ["docker", "rm", "--force"]:
                    self.assertTrue(homes[0].exists(), "remove container before deleting its home")
                return CommandResult(0, "{}", "", 0.1)

            with (
                patch("agent_bench.runner.platform.system", return_value="Linux"),
                patch("agent_bench.runner.os.getuid", return_value=1001),
                patch("agent_bench.runner.os.getgid", return_value=1002),
                patch("agent_bench.runner.run_command", side_effect=command),
            ):
                Docker(RunConfig(model="coder")).execute(
                    image="agent",
                    mounts=mounts,
                    command=["pi"],
                    environment=environment,
                    timeout=2,
                )
            self.assertEqual(mounts, [(config_file, "/home/bench/.pi/agent/models.json", True)])
            self.assertEqual(
                environment, {"BENCH_API_KEY": "environment-only-secret", "HOME": "/home/bench"}
            )
            self.assertFalse(homes[0].parent.exists())
            self.assertEqual(calls[-1][0][:3], ["docker", "rm", "--force"])

    def test_root_hosts_and_non_linux_keep_image_nonroot_user(self):
        for system, uid in (("Linux", 0), ("Darwin", 501)):
            calls = []

            def command(argv, *args, **kwargs):
                calls.append(argv)
                return CommandResult(0, "{}", "", 0.1)

            with (
                self.subTest(system=system, uid=uid),
                patch("agent_bench.runner.platform.system", return_value=system),
                patch("agent_bench.runner.os.getuid", return_value=uid),
                patch("agent_bench.runner.os.getgid") as gid,
                patch("agent_bench.runner.run_command", side_effect=command),
            ):
                Docker(RunConfig(model="coder")).execute(
                    image="agent",
                    mounts=[],
                    command=["pi"],
                    timeout=2,
                )
                self.assertNotIn("--user", calls[0])
                self.assertNotIn("--mount", calls[0])
                gid.assert_not_called()

    def test_linux_grading_keeps_image_user_and_has_no_writable_home(self):
        calls = []

        def command(argv, *args, **kwargs):
            calls.append(argv)
            return CommandResult(0, "{}", "", 0.1)

        with (
            patch("agent_bench.runner.platform.system", return_value="Linux"),
            patch("agent_bench.runner.os.getuid", return_value=1001),
            patch("agent_bench.runner.run_command", side_effect=command),
        ):
            Docker(RunConfig(model="coder")).execute(
                image="grader",
                mounts=[],
                command=["python", "-I"],
                timeout=2,
                grade=True,
            )
        self.assertNotIn("--user", calls[0])
        self.assertNotIn("--mount", calls[0])
        self.assertNotIn("--env", calls[0])
        self.assertIn("--read-only", calls[0])
        self.assertEqual(calls[0][calls[0].index("--network") + 1], "none")

    def test_linux_private_home_is_removed_after_agent_interrupt(self):
        homes = []

        def command(argv, *args, **kwargs):
            if argv[:2] == ["docker", "run"]:
                specs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
                home_spec = next(spec for spec in specs if spec.endswith("target=/home/bench"))
                homes.append(Path(home_spec.split("source=", 1)[1].split(",", 1)[0]))
                raise KeyboardInterrupt
            self.assertTrue(homes[0].exists())
            return CommandResult(0, "", "", 0.1)

        with (
            patch("agent_bench.runner.platform.system", return_value="Linux"),
            patch("agent_bench.runner.os.getuid", return_value=1001),
            patch("agent_bench.runner.os.getgid", return_value=1001),
            patch("agent_bench.runner.run_command", side_effect=command),
            self.assertRaises(KeyboardInterrupt),
        ):
            Docker(RunConfig(model="coder")).execute(
                image="agent",
                mounts=[],
                command=["pi"],
                timeout=2,
            )
        self.assertFalse(homes[0].parent.exists())

    def test_cleanup_failure_is_fatal_and_prevents_source_snapshot(self):
        with patch(
            "agent_bench.runner.run_command",
            return_value=CommandResult(1, "", "daemon unavailable", 0.1),
        ):
            with self.assertRaises(ContainerCleanupError):
                Docker(RunConfig(model="coder")).remove("example")
        with patch(
            "agent_bench.runner.run_command",
            return_value=CommandResult(1, "", "No such container: example", 0.1),
        ):
            Docker(RunConfig(model="coder")).remove("example")
        docker = FakeDocker()
        with (
            patch.object(
                docker, "execute", side_effect=ContainerCleanupError("shutdown unconfirmed")
            ),
            patch("agent_bench.runner.snapshot_sources") as snapshot,
        ):
            result = run_attempt(
                docker, RunConfig(model="coder"), TASKS["bug-fix"], "pi", 1, "a", "b"
            )
        self.assertTrue(result["abort_run"])
        self.assertEqual(result["status"], "error")
        snapshot.assert_not_called()

    def test_interrupt_retains_current_attempt_diagnostics(self):
        docker = FakeDocker(agent=CommandResult(0, "retained-agent-output", "", 0.1))
        original = docker.execute

        def execute(**kwargs):
            if kwargs.get("grade"):
                raise KeyboardInterrupt()
            return original(**kwargs)

        with patch.object(docker, "execute", side_effect=execute):
            result = run_attempt(
                docker, RunConfig(model="coder"), TASKS["bug-fix"], "pi", 1, "a", "b"
            )
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["logs"]["stdout"], "retained-agent-output")
        self.assertIn("patch", result)

    def test_fast_exiting_output_still_exceeds_limit(self):
        result = run_command(["python3", "-c", "print('x'*10000)"], 5, max_output=100)
        self.assertTrue(result.output_limited)
        self.assertIn("truncated", result.stdout)

    def test_subprocess_bounds(self):
        result = run_command(["python3", "-c", "print('hello')"], 5)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.returncode, 0)
        result = run_command(["python3", "-c", "import time; time.sleep(5)"], 0.1)
        self.assertTrue(result.timed_out)
        result = run_command(
            ["python3", "-u", "-c", "import time; print('x'*5000); time.sleep(2)"],
            5,
            max_output=100,
        )
        self.assertTrue(result.output_limited)
        self.assertIn("truncated", result.stdout)

    def test_orchestration_schedule_and_two_files(self):
        config = RunConfig(
            model="coder",
            harnesses=["pi", "opencode"],
            tasks=["bug-fix", "performance"],
            repeats=2,
            build=False,
        )
        schedule = []

        def attempt(docker, config, task, harness, repeat, image, grader):
            schedule.append((harness, task.id, repeat, image, grader))
            return {
                "harness": harness,
                "task_id": task.id,
                "status": "failed",
                "score": 50,
                "duration_seconds": 0.1,
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("agent_bench.runner.Docker") as docker,
            patch("agent_bench.runner.run_attempt", side_effect=attempt),
        ):
            config.output = temp
            docker.return_value.available.return_value = {}
            docker.return_value.image_metadata.side_effect = lambda tag: {
                "tag": tag,
                "id": "immutable:" + tag,
            }
            report, output = run_benchmark(config)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(len(schedule), 8)
            self.assertEqual(len(set((h, t, r) for h, t, r, _, _ in schedule)), 8)
            self.assertTrue(all(image.startswith("immutable:") for _, _, _, image, _ in schedule))
            self.assertEqual({p.name for p in output.iterdir()}, {"results.json", "results.html"})
            self.assertEqual(report["summary"]["mean_score"], 50)
            self.assertEqual(len(report["summary"]["per_harness"]), 2)
            self.assertEqual(report["resolved_tasks"], ["bug-fix", "performance"])
            self.assertEqual(report["task_set_manifest"], [])
            self.assertEqual(report["summary"]["per_task_set"], {})
            self.assertTrue(all(item["task_sets"] == [] for item in report["results"]))

    def test_task_sets_schedule_deduplicates_and_persists_membership(self):
        def attempt(docker, config, task, harness, repeat, image, grader):
            return {
                "harness": harness,
                "task_id": task.id,
                "repeat": repeat,
                "status": "success",
                "score": 100,
                "duration_seconds": 0.1,
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("agent_bench.runner.Docker") as docker,
            patch("agent_bench.runner.run_attempt", side_effect=attempt) as execution,
        ):
            docker.return_value.available.return_value = {}
            docker.return_value.image_metadata.side_effect = lambda tag: {"tag": tag, "id": tag}
            config = RunConfig(
                model="coder",
                harnesses=["pi", "opencode"],
                task_sets=["default", "core"],
                tasks=["performance"],
                repeats=2,
                output=temp,
                build=False,
            )
            report, output = run_benchmark(config)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["resolved_tasks"], list(TASKS))
            self.assertEqual([s["id"] for s in report["task_set_manifest"]], ["default", "core"])
            self.assertEqual(report["task_set_manifest"][1]["task_ids"], list(TASKS)[:3])
            self.assertEqual(execution.call_count, 20)
            self.assertEqual(report["summary"]["attempts"], 20)
            self.assertEqual(report["summary"]["per_task_set"]["default"]["attempts"], 20)
            self.assertEqual(report["summary"]["per_task_set"]["core"]["attempts"], 12)
            self.assertEqual(
                report["summary"]["per_task_set"]["core"]["per_harness"]["pi"]["attempts"], 6
            )
            expected_membership = {
                task_id: ["default", "core"] if task_id in list(TASKS)[:3] else ["default"]
                for task_id in TASKS
            }
            for item in [*report["schedule"], *report["results"]]:
                self.assertEqual(item["task_sets"], expected_membership[item["task_id"]])
            self.assertEqual(
                len({(r["harness"], r["task_id"], r["repeat"]) for r in report["results"]}), 20
            )
            self.assertEqual(json.loads((output / "results.json").read_text()), report)
            html = (output / "results.html").read_text()
            self.assertIn("Task-set-by-harness comparison", html)
            self.assertIn("Core", html)
            self.assertEqual({p.name for p in output.iterdir()}, {"results.json", "results.html"})

    def test_orchestration_prints_and_persists_warning_without_changing_run_status(self):
        for warning in (None, "Final response-token budget exhausted; diagnostic only"):
            result = {
                "harness": "pi",
                "task_id": "bug-fix",
                "status": "success",
                "score": 100,
                "duration_seconds": 0.1,
                "termination": {
                    "reason": "length" if warning else "stop",
                    "output_tokens": 4096,
                    "reasoning_tokens": None,
                    "warning": warning,
                },
            }
            with (
                self.subTest(warning=warning),
                tempfile.TemporaryDirectory() as temp,
                patch("agent_bench.runner.Docker") as docker,
                patch("agent_bench.runner.run_attempt", return_value=result),
                patch("sys.stdout", new_callable=io.StringIO) as console,
            ):
                docker.return_value.available.return_value = {}
                docker.return_value.image_metadata.side_effect = lambda tag: {"tag": tag, "id": tag}
                config = RunConfig(
                    model="coder",
                    harnesses=["pi"],
                    tasks=["bug-fix"],
                    api_profile="openai-compatible",
                    output=temp,
                    build=False,
                )
                report, output = run_benchmark(config)
                self.assertEqual(report["status"], "completed")
                self.assertEqual(report["summary"]["mean_score"], 100)
                self.assertEqual(report["summary"]["success_rate"], 1)
                self.assertEqual(report["model"]["api_profile"], "openai-compatible")
                self.assertEqual(report["parameters"]["api_profile"], "openai-compatible")
                persisted = json.loads((output / "results.json").read_text())
                self.assertEqual(persisted["results"][0]["termination"], result["termination"])
                self.assertIn("Final response:", console.getvalue())
                self.assertIn("output tokens=4096, reasoning tokens=n/a", console.getvalue())
                if warning:
                    self.assertIn("WARNING: " + warning, console.getvalue())
                    self.assertIn(warning, (output / "results.html").read_text())
                else:
                    self.assertNotIn("WARNING:", console.getvalue())

    def test_interruption_and_cleanup_abort_stop_schedule(self):
        for status, extra, expected in (
            ("interrupted", {}, "interrupted"),
            ("error", {"abort_run": True}, "error"),
        ):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as temp,
                patch("agent_bench.runner.Docker") as docker,
                patch("agent_bench.runner.run_attempt") as attempt,
            ):
                docker.return_value.available.return_value = {}
                docker.return_value.image_metadata.side_effect = lambda tag: {"tag": tag, "id": tag}
                attempt.return_value = {
                    "harness": "pi",
                    "task_id": "bug-fix",
                    "status": status,
                    "score": 0,
                    "duration_seconds": 0.1,
                    "error": "stopped",
                    **extra,
                }
                report, _ = run_benchmark(RunConfig(model="coder", output=temp, build=False))
                self.assertEqual(report["status"], expected)
                self.assertEqual(len(report["results"]), 1)
                self.assertEqual(report["summary"]["mean_score"], 0)
                self.assertEqual(attempt.call_count, 1)

    def test_setup_failure_still_writes_reports(self):
        with tempfile.TemporaryDirectory() as temp, patch("agent_bench.runner.Docker") as docker:
            docker.return_value.available.side_effect = RuntimeError("Docker is offline")
            report, output = run_benchmark(RunConfig(model="coder", output=temp))
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["error"], "Docker is offline")
            self.assertTrue((output / "results.json").is_file())
            self.assertTrue((output / "results.html").is_file())


if __name__ == "__main__":
    unittest.main()

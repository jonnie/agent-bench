"""Offline regressions for artifact hygiene and installed-only smoke isolation."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_distribution.py"
SPEC = importlib.util.spec_from_file_location("check_distribution", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def wheel(self, *, omit=None, extra=()):
        artifact = self.directory / "agent_bench-0.1.0-py3-none-any.whl"
        payload = checker.REQUIRED_PACKAGE_FILES | {"tasks/new_task.py", "task_sets/new_set.py"}
        names = {"agent_bench/" + name for name in payload}
        names.add("agent_bench-0.1.0.dist-info/licenses/LICENSE")
        names.update(extra)
        with zipfile.ZipFile(artifact, "w") as archive:
            for name in sorted(names - {omit}):
                archive.writestr(name, "fixture")
        return artifact, payload

    def sdist(self, *, omit=None, extra=(), link=False):
        artifact = self.directory / "agent_bench-0.1.0.tar.gz"
        payload = checker.REQUIRED_PACKAGE_FILES | {"tasks/new_task.py", "task_sets/new_set.py"}
        names = {"src/agent_bench/" + name for name in payload} | checker.REQUIRED_RELEASE_FILES
        names.update(extra)
        with tarfile.open(artifact, "w:gz") as archive:
            for name in sorted(names - {omit}):
                archive.addfile(tarfile.TarInfo("agent_bench-0.1.0/" + name))
            if link:
                member = tarfile.TarInfo("agent_bench-0.1.0/link")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                archive.addfile(member)
        return artifact, payload

    def test_wheel_and_sdist_include_new_tasks_sets_and_resources(self):
        for create in (self.wheel, self.sdist):
            with self.subTest(kind=create.__name__):
                artifact, payload = create()
                self.assertEqual(checker.check_archive(artifact), payload)

    def test_missing_schema_or_dockerfile_is_rejected(self):
        for name in ("schemas/results-v1.schema.json", "docker/Dockerfile.pi"):
            with self.subTest(name=name):
                artifact, _ = self.wheel(omit="agent_bench/" + name)
                with self.assertRaisesRegex(ValueError, "missing package files"):
                    checker.check_archive(artifact)

    def test_missing_wheel_license_is_rejected(self):
        artifact, _ = self.wheel(omit="agent_bench-0.1.0.dist-info/licenses/LICENSE")
        with self.assertRaisesRegex(ValueError, "missing wheel license"):
            checker.check_archive(artifact)

    def test_missing_sdist_docs_and_examples_are_rejected(self):
        for name in (
            "docs/REPORT_FORMAT.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "examples/qwen3.8-27b/results.json",
            "examples/qwen3.8-27b/results.html",
        ):
            with self.subTest(name=name):
                artifact, _ = self.sdist(omit=name)
                with self.assertRaisesRegex(ValueError, "missing sdist docs/examples/files"):
                    checker.check_archive(artifact)

    def test_local_outputs_and_secrets_are_rejected(self):
        for name in (
            "results/run.json",
            ".venv/bin/python",
            "build/lib/module.py",
            "dist/old.whl",
            ".env",
            ".env.local",
            "private.key",
            "token.pem",
            "agent_bench/__pycache__/cli.pyc",
        ):
            for create in (self.wheel, self.sdist):
                with self.subTest(name=name, kind=create.__name__):
                    artifact, _ = create(extra=(name,))
                    with self.assertRaisesRegex(ValueError, "forbidden release file"):
                        checker.check_archive(artifact)

    def test_unsafe_paths_and_sdist_links_are_rejected(self):
        for create in (self.wheel, self.sdist):
            artifact, _ = create(extra=("../outside",))
            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                checker.check_archive(artifact)
        artifact, _ = self.sdist(link=True)
        with self.assertRaisesRegex(ValueError, "archive links"):
            checker.check_archive(artifact)

    def test_environment_does_not_inherit_checkout_or_model_settings(self):
        with patch.dict(
            os.environ,
            {
                "PYTHONPATH": "/checkout/src",
                "PYTHONHOME": "/wrong-python",
                "AGENT_BENCH_API_KEY": "not-used",
                "OPENAI_API_KEY": "not-used",
                "VIRTUAL_ENV": "/old-venv",
            },
        ):
            environment = checker.clean_environment()
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "AGENT_BENCH_API_KEY",
            "OPENAI_API_KEY",
            "VIRTUAL_ENV",
        ):
            self.assertNotIn(name, environment)

    def test_pipless_symlink_venv_and_isolated_installed_smoke(self):
        artifact, payload = self.wheel()
        response = {"version": "0.1.0", "catalog": {}}
        with (
            patch.object(checker.venv, "EnvBuilder") as builder,
            patch.object(
                checker.subprocess,
                "run",
                side_effect=[
                    Mock(returncode=0),
                    Mock(returncode=0, stdout=json.dumps(response)),
                ],
            ) as run,
        ):
            self.assertEqual(checker.smoke_install(artifact, payload), response)
        builder.assert_called_once_with(with_pip=False, symlinks=True)
        install, smoke = run.call_args_list
        self.assertIn("--python", install.args[0])
        self.assertIn("--no-deps", install.args[0])
        self.assertEqual(smoke.args[0][1:3], ["-I", "-c"])
        self.assertNotEqual(smoke.kwargs["cwd"], SCRIPT.parent.parent)
        self.assertEqual(json.loads(smoke.args[0][-1]), sorted(payload))
        self.assertNotIn("PYTHONPATH", smoke.kwargs["env"])

    def test_mismatched_artifacts_or_install_failure_returns_nonzero(self):
        first, payload = self.wheel()
        second, _ = self.sdist()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(
                checker,
                "smoke_install",
                side_effect=[
                    {"version": "1"},
                    {"version": "2"},
                ],
            ):
                self.assertEqual(checker.main([str(first), str(second)]), 1)
            with patch.object(
                checker,
                "smoke_install",
                side_effect=subprocess.CalledProcessError(
                    1,
                    ["python"],
                    stderr="installed import failed",
                ),
            ):
                self.assertEqual(checker.main([str(first)]), 1)
        self.assertTrue(payload)


if __name__ == "__main__":
    unittest.main()

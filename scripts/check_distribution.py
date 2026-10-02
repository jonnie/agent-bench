"""Check wheel/sdist contents and clean installs without Docker or model calls.

Usage: python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
Requires pip >= 22.3 in the invoking interpreter (for pip --python). Only sdist
build isolation may fetch build-backend dependencies; smoke checks are offline.
Do not run this on untrusted artifacts: installing an sdist executes its backend.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path, PurePosixPath

DOCKERFILES = {f"docker/Dockerfile.{name}" for name in ("base", "pi", "oh-my-pi", "opencode")}
REQUIRED_PACKAGE_FILES = DOCKERFILES | {
    "__init__.py",
    "__main__.py",
    "cli.py",
    "report.py",
    "runner.py",
    "docker/__init__.py",
    "tasks/__init__.py",
    "task_sets/__init__.py",
    "schemas/__init__.py",
    "schemas/results-v1.schema.json",
}
REQUIRED_RELEASE_FILES = {
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "MANIFEST.in",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "benchmark.example.json",
    "docs/REPORT_FORMAT.md",
    "docs/RELEASING.md",
    "docs/GITHUB_SETUP.md",
    "docs/releases/0.1.0.md",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/dependabot.yml",
    ".github/main-ruleset.json",
    "scripts/check_distribution.py",
    "scripts/make_example.py",
    "examples/README.md",
    "examples/qwen3.8-27b/results.json",
    "examples/qwen3.8-27b/results.html",
}
FORBIDDEN_DIRECTORIES = {
    "results",
    ".venv",
    "venv",
    "build",
    "dist",
    ".git",
    "__pycache__",
    ".ruff_cache",
}

# Executed by the installed interpreter with -I, from an empty temporary cwd.
# Nothing from the checkout is imported or added to sys.path.
SMOKE_CODE = r"""
import importlib
from importlib import metadata, resources
import json
from pathlib import Path
import subprocess
import sys

import agent_bench
from agent_bench.cli import parser, run_configuration
from agent_bench.task_sets import TASK_SETS
from agent_bench.tasks import TASKS

prefix = Path(sys.prefix).resolve()
assert Path(agent_bench.__file__).resolve().is_relative_to(prefix), agent_bench.__file__
dist = metadata.distribution("agent-bench")
assert dist.version == agent_bench.__version__
assert dist.metadata["Requires-Python"] == ">=3.11"
assert dist.metadata["License-Expression"] == "MIT"
assert "LICENSE" in dist.metadata.get_all("License-File", [])
assert "Development Status :: 3 - Alpha" in dist.metadata.get_all("Classifier", [])
assert "Environment :: Console" in dist.metadata.get_all("Classifier", [])
assert all("extra ==" in requirement for requirement in dist.requires or []), dist.requires
assert dist.read_text("licenses/LICENSE") is not None

expected = json.loads(sys.argv[1])
root = resources.files("agent_bench")
for name in expected:
    resource = root.joinpath(*name.split("/"))
    assert resource.is_file(), name
    if name.endswith(".json"):
        schema = json.loads(resource.read_text(encoding="utf-8"))
        if name == "schemas/results-v1.schema.json":
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert schema["properties"]["schema_version"]["const"] == 1
    elif name.endswith(".py") and name != "__main__.py":
        module = "agent_bench." + name[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        importlib.import_module(module)
    elif name.startswith("docker/"):
        assert resource.read_text(encoding="utf-8").strip(), name

executable = prefix / ("Scripts/agent-bench.exe" if sys.platform == "win32" else "bin/agent-bench")
def cli(*arguments):
    result = subprocess.run([str(executable), *arguments], check=True,
                            capture_output=True, text=True, timeout=30)
    return result.stdout

assert "Harnesses:" in cli("list")
catalog = json.loads(cli("list", "--json"))
assert {task["id"] for task in catalog["tasks"]} == set(TASKS)
assert {group["id"] for group in catalog["task_sets"]} == set(TASK_SETS)
assert catalog["harnesses"]
assert agent_bench.__version__ in cli("--version")
subprocess.run([sys.executable, "-I", "-m", "agent_bench", "--help"],
               check=True, capture_output=True, timeout=30)

# Resolve every catalog entry without ever dispatching the run command.
for task_id in TASKS:
    config = run_configuration(parser().parse_args(
        ["run", "--model", "offline-smoke", "--tasks", task_id, "--no-build"]))
    assert [task.id for task in config.selected_tasks()] == [task_id]
for group in TASK_SETS.values():
    config = run_configuration(parser().parse_args(
        ["run", "--model", "offline-smoke", "--task-sets", group.id, "--no-build"]))
    assert [task.id for task in config.selected_tasks()] == list(group.task_ids)
config = run_configuration(parser().parse_args(
    ["run", "--model", "offline-smoke", "--task-sets", *TASK_SETS,
     "--tasks", *TASKS, "--no-build"]))
selected = [task.id for task in config.selected_tasks()]
assert len(selected) == len(set(selected)) == len(TASKS)

# Legacy reports remain renderable; schema validity of producer reports is
# exercised separately by the jsonschema dev tests, not a runtime dependency.
report = {"schema_version": 1, "run_id": "distribution-smoke", "status": "completed",
          "model": {"id": "offline-smoke"}, "results": []}
source = Path("fixture.json")
source.write_text(json.dumps(report), encoding="utf-8")
cli("report", str(source), "--output", "rendered")
saved = json.loads(Path("rendered/results.json").read_text(encoding="utf-8"))
assert saved["run_id"] == report["run_id"]
assert saved["summary"]["attempts"] == 0
html = Path("rendered/results.html").read_text(encoding="utf-8")
assert "offline-smoke" in html and "<html" in html.lower()
assert "<script" not in html.lower()
print(json.dumps({"version": dist.version, "catalog": catalog}, sort_keys=True))
"""


def check_archive(artifact: Path) -> set[str]:
    """Return package payload paths, rejecting incomplete or dirty releases."""
    if artifact.name.endswith(".whl"):
        with zipfile.ZipFile(artifact) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
        prefix = "agent_bench/"
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
            raise ValueError(f"{artifact.name}: missing wheel license")
    elif artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError(f"{artifact.name}: archive links are not allowed")
        paths = [PurePosixPath(member.name) for member in members if member.isfile()]
        for path in paths:
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{artifact.name}: unsafe archive path: {path}")
        roots = {path.parts[0] for path in paths if path.parts}
        if len(roots) != 1:
            raise ValueError(f"{artifact.name}: expected one sdist root")
        names = [str(path.relative_to(next(iter(roots)))) for path in paths]
        missing = REQUIRED_RELEASE_FILES - set(names)
        if missing:
            raise ValueError(
                f"{artifact.name}: missing sdist docs/examples/files: {sorted(missing)}"
            )
        prefix = "src/agent_bench/"
    else:
        raise ValueError(f"Unsupported artifact: {artifact}; expected .whl or .tar.gz")

    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"{artifact.name}: unsafe archive path: {name}")
        if (
            FORBIDDEN_DIRECTORIES.intersection(path.parts)
            or path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix in {".pyc", ".pyo", ".pem", ".key"}
        ):
            raise ValueError(f"{artifact.name}: forbidden release file: {name}")
    payload = {name.removeprefix(prefix) for name in names if name.startswith(prefix)}
    missing = REQUIRED_PACKAGE_FILES - payload
    if missing:
        raise ValueError(f"{artifact.name}: missing package files: {sorted(missing)}")
    return payload


def clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Avoid source-tree import injection and interpreter/venv overrides. Smoke
    # code neither reads credentials nor contacts a server; also drop common keys.
    for name in list(environment):
        if name.startswith(("PYTHON", "AGENT_BENCH_")) or name in {
            "VIRTUAL_ENV",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }:
            environment.pop(name)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def smoke_install(artifact: Path, payload: set[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="agent-bench-distribution-") as temporary:
        directory = Path(temporary)
        target = directory / "venv"
        # uv-managed Python needs its real stdlib beside the symlink target;
        # copying its executable can produce a fatal missing-encodings error.
        venv.EnvBuilder(with_pip=False, symlinks=True).create(target)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cwd = directory / "smoke"
        cwd.mkdir()
        environment = clean_environment()
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "--python",
                str(python),
                "install",
                "--no-deps",
                "--no-cache-dir",
                str(artifact.resolve()),
            ],
            cwd=cwd,
            env=environment,
            check=True,
            timeout=300,
        )
        result = subprocess.run(
            [str(python), "-I", "-c", SMOKE_CODE, json.dumps(sorted(payload))],
            cwd=cwd,
            env=environment,
            check=True,
            timeout=120,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts", nargs="+", type=Path, help="Built wheels and/or .tar.gz sdists"
    )
    arguments = parser.parse_args(argv)
    baseline: tuple[set[str], dict] | None = None
    try:
        for artifact in arguments.artifacts:
            payload = check_archive(artifact)
            result = smoke_install(artifact, payload)
            if baseline is not None and baseline != (payload, result):
                raise ValueError(f"{artifact.name}: distributions have different payloads/catalogs")
            baseline = payload, result
            print(
                f"OK: {artifact.name} (archive, clean install, CLI, selection, reports, resources)"
            )
    except (
        OSError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Distribution check failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Host orchestration. Candidate code is only executed by Docker, never the host."""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import platform
import random
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__
from .harnesses import (
    HARNESS_VERSIONS,
    harness_error,
    image_tag,
    parse_termination,
    parse_usage,
    prepare_harness,
)
from .report import describe_outcome, summarize, write_reports
from .task_sets import DEFAULT_TASK_SETS, TaskSet, get_task_sets, resolve_tasks
from .tasks import Task

DOCKERFILES = Path(__file__).parent / "docker"
BASE_IMAGE = "agent-bench-base:local"


@dataclass
class RunConfig:
    model: str
    base_url: str = "http://host.docker.internal:8080/v1"
    harnesses: list[str] = field(default_factory=lambda: list(HARNESS_VERSIONS))
    tasks: list[str] | None = None
    task_sets: list[str] | None = None
    output: str = "results"
    repeats: int = 1
    timeout: float | None = 600
    grade_timeout: float = 60
    build_timeout: float = 900
    cpus: float = 2
    memory: str = "4g"
    pids_limit: int = 256
    context_window: int = 32768
    max_tokens: int = 4096
    reasoning: bool = False
    thinking: str = "off"
    api_profile: str = "llama-cpp"
    tool_profile: str = "native"
    seed: int = 42
    api_key_env: str = "AGENT_BENCH_API_KEY"
    input_price: float | None = None
    output_price: float | None = None
    network: str = "bridge"
    platform: str | None = None
    build: bool = True
    max_log_bytes: int | None = 2_000_000
    max_source_bytes: int = 2_000_000
    harness_versions: dict[str, str] = field(default_factory=lambda: dict(HARNESS_VERSIONS))

    def selected_tasks(self) -> list[Task]:
        return resolve_tasks(tasks=self.tasks, task_sets=self.task_sets)

    def selected_task_sets(self) -> list[TaskSet]:
        ids: list[str] | None = (
            list(DEFAULT_TASK_SETS)
            if self.tasks is None and self.task_sets is None
            else self.task_sets
        )
        return get_task_sets(ids) if ids is not None else []

    def validate(self):
        self.base_url = normalize_base_url(self.base_url)
        if not isinstance(self.harnesses, list) or not all(
            isinstance(item, str) for item in self.harnesses
        ):
            raise ValueError("harnesses must be a list of names")
        if not isinstance(self.harness_versions, dict):
            raise ValueError("harness_versions must be a mapping")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        if type(self.build) is not bool:
            raise ValueError("build must be a boolean")
        if not isinstance(self.output, str) or not self.output:
            raise ValueError("output must be a nonempty directory path")
        if not self.harnesses or len(set(self.harnesses)) != len(self.harnesses):
            raise ValueError("Select at least one harness, without duplicates")
        for harness in self.harnesses:
            if harness not in HARNESS_VERSIONS:
                raise ValueError(f"Unknown harness: {harness}")
            image_tag(harness, self.harness_versions[harness])
        self.selected_tasks()
        for name in ("repeats", "pids_limit", "max_source_bytes", "max_log_bytes"):
            value = getattr(self, name)
            if name == "max_log_bytes" and value is None:
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout", "grade_timeout", "build_timeout", "cpus"):
            value = getattr(self, name)
            if name == "timeout" and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        import re

        if not isinstance(self.memory, str) or not re.fullmatch(
            r"[1-9][0-9]*(?:[bkmgBKMG])?", self.memory
        ):
            raise ValueError("memory must be a positive Docker size, e.g. 4g or 2048m")
        if (
            not isinstance(self.network, str)
            or self.network == "none"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.network)
        ):
            raise ValueError(
                "Agent networking must reach the model; use bridge, host, or a Docker network name"
            )
        if self.platform is not None and (
            not isinstance(self.platform, str)
            or not re.fullmatch(r"linux/(amd64|arm64)(?:/v[0-9]+)?", self.platform)
        ):
            raise ValueError("platform must be linux/amd64 or linux/arm64 (optionally /vN)")
        # Validate adapter options without storing or exposing a credential.
        from .harnesses import _configuration

        _configuration(asdict(self))
        return self


def normalize_base_url(value: str) -> str:
    """Accept a server root or OpenAI API root, and append /v1 once."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("base URL must be an HTTP(S) URL")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(c in value for c in "${}")
    ):
        raise ValueError("base URL must be HTTP(S), without credentials, query, or fragment")
    _ = parts.port
    path = parts.path.rstrip("/")
    if path.endswith(("/chat/completions", "/models")):
        raise ValueError("Use the server URL or API root, not /models or /chat/completions")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_limited: bool = False


def _read_log(stream, limit: int | None) -> str:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if limit is None or size <= limit:
        return stream.read().decode("utf-8", errors="replace")
    first = stream.read(limit // 2)
    stream.seek(-limit // 2, os.SEEK_END)
    last = stream.read(limit // 2)
    return (
        first.decode("utf-8", errors="replace")
        + "\n[output truncated]\n"
        + last.decode("utf-8", errors="replace")
    )


def run_command(
    command: list[str],
    timeout: float | None,
    *,
    env: dict | None = None,
    max_output: int | None = 2_000_000,
    live: bool = False,
) -> CommandResult:
    """Run without a shell; None disables either limit independently.

    Logs spool to host temporary files. Unlimited capture can exhaust host disk
    and requires loading the complete output into memory when the process ends.
    """
    start = time.monotonic()
    timed_out = limited = False
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env
        )
        try:
            while process.poll() is None:
                if timeout is not None and time.monotonic() - start >= timeout:
                    timed_out = True
                    break
                if (
                    not live
                    and max_output is not None
                    and (
                        os.fstat(out.fileno()).st_size > max_output
                        or os.fstat(err.fileno()).st_size > max_output
                    )
                ):
                    limited = True
                    break
                time.sleep(0.05)
            if timed_out or limited:
                process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if (
            not live
            and max_output is not None
            and (
                os.fstat(out.fileno()).st_size > max_output
                or os.fstat(err.fileno()).st_size > max_output
            )
        ):
            limited = True
        stdout, stderr = _read_log(out, max_output), _read_log(err, max_output)
    # Build logs are shown after completion and may be truncated; runtime logs go
    # only into reports, where the API key is redacted before persistence.
    if live:
        print(stdout, end="", flush=True)
        print(stderr, end="", flush=True)
    return CommandResult(
        process.returncode, stdout, stderr, time.monotonic() - start, timed_out, limited
    )


class ContainerCleanupError(RuntimeError):
    """Container shutdown is unconfirmed; do not read source or schedule more agents."""


class Docker:
    def __init__(self, config: RunConfig):
        self.config = config

    def checked(self, args: list[str], timeout: float = 30, live: bool = False) -> CommandResult:
        result = run_command(
            ["docker", *args], timeout, max_output=self.config.max_log_bytes, live=live
        )
        if result.timed_out:
            raise RuntimeError(f"Docker command timed out after {timeout}s: {' '.join(args[:4])}")
        if result.returncode or result.output_limited:
            raise RuntimeError(
                f"Docker command failed: {' '.join(args[:4])}\n{result.stderr or result.stdout}"
            )
        return result

    def available(self) -> dict:
        if not shutil.which("docker"):
            raise RuntimeError(
                "Docker CLI not found. Install Docker Desktop or Docker Engine and start it."
            )
        version = self.checked(["version", "--format", "{{json .}}"])
        info = self.checked(["info", "--format", "{{json .}}"])
        data = json.loads(info.stdout)
        return {
            "version": json.loads(version.stdout),
            "daemon": {
                key: data.get(key)
                for key in ("OSType", "Architecture", "OperatingSystem", "NCPU", "MemTotal")
            },
        }

    def build_images(self):
        selected = [(BASE_IMAGE, "base", None)] + [
            (image_tag(h, self.config.harness_versions[h]), h, self.config.harness_versions[h])
            for h in self.config.harnesses
        ]
        for tag, name, version in selected:
            print(f"Building {tag} …", flush=True)
            args = ["build", "-t", tag, "-f", str(DOCKERFILES / f"Dockerfile.{name}")]
            if self.config.platform:
                args += ["--platform", self.config.platform]
            if version:
                args += [
                    "--build-arg",
                    f"HARNESS_VERSION={version}",
                    "--build-arg",
                    f"BASE_IMAGE={BASE_IMAGE}",
                ]
            args.append(str(DOCKERFILES))
            self.checked(args, self.config.build_timeout, live=True)

    def image_metadata(self, image: str) -> dict:
        record = json.loads(self.checked(["image", "inspect", image]).stdout)[0]
        return {
            "tag": image,
            "id": record["Id"],
            "repo_digests": record.get("RepoDigests", []),
            "architecture": record.get("Architecture"),
            "os": record.get("Os"),
            "created": record.get("Created"),
        }

    def remove(self, name: str):
        # Killing the Docker client does not kill a running container. Always
        # force-remove it, including on Ctrl-C and output/time limit exhaustion.
        result = run_command(["docker", "rm", "--force", name], 15, max_output=16384)
        if result.returncode == 0 and not result.timed_out:
            return
        if not result.timed_out and f"No such container: {name}" in result.stderr:
            return  # --rm already removed the stopped container.
        raise ContainerCleanupError(
            f"Could not confirm container cleanup for {name}. Stop it before continuing. "
            f"{result.stderr or result.stdout or 'Docker removal timed out'}"
        )

    def limits(self) -> list[str]:
        c = self.config
        args = [
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(c.pids_limit),
            "--cpus",
            str(c.cpus),
            "--memory",
            c.memory,
            "--memory-swap",
            c.memory,
            "--log-driver",
            "none",
        ]
        if c.platform:
            args += ["--platform", c.platform]
        return args

    def execute(
        self,
        *,
        image: str,
        mounts: list[tuple[Path, str, bool]],
        command: list[str],
        timeout: float | None,
        environment: dict | None = None,
        grade: bool = False,
    ) -> CommandResult:
        name = f"agent-bench-{'grade' if grade else 'agent'}-{uuid.uuid4().hex[:16]}"
        args = ["docker", "run", "--rm", "--name", name, *self.limits()]
        with ExitStack() as stack:
            run_mounts = list(mounts)
            run_environment = dict(environment or {})
            if grade:
                args += [
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=256m",
                    "--workdir",
                    "/candidate",
                ]
            else:
                args += ["--network", self.config.network, "--workdir", "/workspace"]
                if self.config.network != "host":
                    args += ["--add-host", "host.docker.internal:host-gateway"]
                if platform.system() == "Linux" and os.getuid() != 0:
                    # Match ownership of every agent-created file, not just Python
                    # caches. chmod on the initial workspace cannot fix later 0700
                    # directories owned by another UID, and umask cannot override
                    # explicit modes. Keep root hosts on the image's nonroot user.
                    args += ["--user", f"{os.getuid()}:{os.getgid()}"]
                    home_root = Path(
                        stack.enter_context(tempfile.TemporaryDirectory(prefix="agent-bench-home-"))
                    )
                    home = home_root / "home"
                    home.mkdir(mode=0o700)
                    # A numeric UID cannot write the image's UID-1000 home. Only
                    # mount a fresh private home, never the caller's actual home.
                    for source, target, _ in mounts:
                        try:
                            relative = PurePosixPath(target).relative_to("/home/bench")
                        except ValueError:
                            continue
                        if ".." in relative.parts:
                            raise ValueError("Harness home mount paths cannot traverse parents")
                        destination = home / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_dir():
                            destination.mkdir(exist_ok=True)
                        else:
                            destination.touch(mode=0o600)
                    run_mounts.insert(0, (home, "/home/bench", False))
                    run_environment["HOME"] = "/home/bench"
            for source, target, readonly in run_mounts:
                if "," in str(source) or "," in target:
                    raise ValueError("Docker bind paths cannot contain commas")
                mount = f"type=bind,source={source.resolve()},target={target}"
                args += ["--mount", mount + (",readonly" if readonly else "")]
            child_env = dict(os.environ)
            for key, value in run_environment.items():
                child_env[key] = value
                args += ["--env", key]  # Values never appear in process arguments.
            args += [image, *command]
            try:
                return run_command(
                    args, timeout, env=child_env, max_output=self.config.max_log_bytes
                )
            finally:
                self.remove(name)


def snapshot_sources(workspace: Path, destination: Path, limit: int) -> dict[str, str]:
    """Only regular UTF-8 Python files under solution enter the clean grader.

    Refuse symlinks/special files before opening, constrain file count/bytes, and
    don't walk into candidate-created directory links. Agent container is stopped.
    """
    result = {}
    total = 0
    root = workspace / "solution"
    if not root.exists() and not root.is_symlink():
        return result
    if root.is_symlink() or not root.is_dir():
        raise ValueError("solution must be a real directory, not a link")
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError("Symlink directories are not permitted in solution")
        for name in sorted(filenames):
            path = Path(current) / name
            if path.suffix != ".py":
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(
                    f"Candidate source is not a regular file: {path.relative_to(workspace)}"
                )
            size = path.stat().st_size
            total += size
            if total > limit or len(result) >= 500:
                raise ValueError("Candidate source exceeds the byte or file limit")
            # O_NOFOLLOW guards a last-component link even if a host user races us.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                contents = source.read(limit + 1)
            if len(contents) > limit:
                raise ValueError("Candidate source file exceeds the byte limit")
            text = contents.decode("utf-8")
            relative = path.relative_to(workspace)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            target.chmod(0o644)
            result[relative.as_posix()] = text
    return result


def patch_metrics(before: dict[str, str], after: dict[str, str]) -> dict:
    before = {k: v for k, v in before.items() if k.startswith("solution/") and k.endswith(".py")}
    diff = []
    changed = added = removed = 0
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, ""), after.get(path, "")
        if old == new and (path in before) == (path in after):
            continue
        changed += 1
        lines = list(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in after else "/dev/null",
                lineterm="",
            )
        )
        # Headers can themselves begin + or -; count only actual edit lines.
        added += sum(line.startswith("+") for line in lines[2:])
        removed += sum(line.startswith("-") for line in lines[2:])
        diff.extend(lines)
    return {
        "files_changed": changed,
        "lines_added": added,
        "lines_removed": removed,
        "diff": "\n".join(diff),
    }


def _redact(value: Any, secret: str | None) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def validate_grade(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Grader did not return a JSON object")
    for key in ("tests_total", "tests_passed", "tests_failed", "tests_errors"):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError(f"Invalid grader count: {key}")
    total = data["tests_total"]
    if total < 1 or total != sum(data[k] for k in ("tests_passed", "tests_failed", "tests_errors")):
        raise ValueError("Grader counts are inconsistent")
    expected = round(100 * data["tests_passed"] / total, 2)
    if type(data.get("score")) not in (int, float) or data["score"] != expected:
        raise ValueError("Grader score does not match passed/total")
    if type(data.get("success")) is not bool or data["success"] != (data["tests_passed"] == total):
        raise ValueError("Grader success flag is inconsistent")
    if not isinstance(data.get("cases"), list) or len(data["cases"]) != total:
        raise ValueError("Grader case count is inconsistent")
    return data


def _output_limit_error(component: str, limit: int | None) -> str:
    if limit is None:
        return (
            f"{component} execution reported an output limit despite --max-log-bytes unlimited; "
            "inspect execution logs"
        )
    return (
        f"{component} exceeded its log output limit "
        f"({limit:,} bytes per stdout/stderr stream; "
        "--max-log-bytes; not a model token limit)"
    )


def run_attempt(
    docker: Docker,
    config: RunConfig,
    task: Task,
    harness: str,
    repeat: int,
    image: str,
    grader_image: str,
) -> dict:
    result = {
        "harness": harness,
        "task_id": task.id,
        "task_title": task.title,
        "category": task.category,
        "repeat": repeat,
        "status": "error",
        "score": 0,
        "duration_seconds": 0,
        "agent_exit_code": None,
        "grading": None,
        "usage": {},
        "termination": {},
        "patch": {},
        "logs": {},
        "started_at": utc_now(),
    }
    started = time.monotonic()
    secret = os.environ.get(config.api_key_env)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-bench-") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir(mode=0o777)
            for relative, contents in task.files.items():
                target = workspace / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
                target.chmod(0o666)
            # Keep fixtures writable for the image user on Docker Desktop/root hosts.
            for current, _, _ in os.walk(workspace):
                Path(current).chmod(0o777)
            adapter = prepare_harness(harness, asdict(config), root / "config")
            result["harness_configuration"] = adapter["configuration"]
            mounts = [(workspace, "/workspace", False)] + [
                (path, target, True) for path, target in adapter["mounts"]
            ]
            prompt = (
                task.prompt
                + "\nWork autonomously in /workspace. Modify production code under solution/; "
                "you may add public tests. Use only the configured model. No delegation, external "
                "services, grading manipulation, or changing the task contract. "
                "Only solution/**/*.py is submitted for independent hidden grading.\n"
            )
            result["prompt"] = prompt
            agent = docker.execute(
                image=image,
                mounts=mounts,
                command=[*adapter["command"], prompt],
                environment=adapter["environment"],
                timeout=config.timeout,
            )
            result.update(
                agent_exit_code=agent.returncode, agent_duration_seconds=agent.duration_seconds
            )
            result["logs"].update(stdout=agent.stdout, stderr=agent.stderr)
            result["usage"] = parse_usage(
                harness, agent.stdout, config.input_price, config.output_price
            )
            result["termination"] = parse_termination(harness, agent.stdout)
            candidate = root / "candidate"
            candidate.mkdir()
            sources = snapshot_sources(workspace, candidate, config.max_source_bytes)
            result["patch"] = patch_metrics(task.files, sources)
            result["source_sha256"] = hashlib.sha256(
                json.dumps(sources, sort_keys=True).encode()
            ).hexdigest()
            grader = root / "grader"
            grader.mkdir()
            shutil.copyfile(Path(__file__).with_name("grader.py"), grader / "grader.py")
            (grader / "hidden_tests.py").write_text(task.hidden_tests, encoding="utf-8")
            grade = docker.execute(
                image=grader_image,
                mounts=[(candidate, "/candidate", True), (grader, "/grader", True)],
                command=[
                    "python",
                    "-I",
                    "-B",
                    "/grader/grader.py",
                    "/candidate",
                    "/grader/hidden_tests.py",
                ],
                timeout=config.grade_timeout,
                grade=True,
            )
            result["logs"].update(grader_stdout=grade.stdout, grader_stderr=grade.stderr)
            result["grade_duration_seconds"] = grade.duration_seconds
            result["grader_exit_code"] = grade.returncode
            observed_error = harness_error(harness, agent.stdout)
            if grade.timed_out:
                result.update(
                    status="timeout",
                    error=f"Grading exceeded its time limit ({config.grade_timeout:g} s; --grade-timeout)",
                )
            elif grade.output_limited:
                result.update(error=_output_limit_error("Grading", config.max_log_bytes))
            elif grade.returncode:
                result.update(
                    error=f"Grading process exited with code {grade.returncode}; inspect grader logs"
                )
            else:
                grading = validate_grade(json.loads(grade.stdout))
                result["grading"] = grading
                result["score"] = grading["score"]
                result["status"] = "success" if grading["success"] else "failed"
            # Failed agent execution never earns successful-attempt credit, even
            # if a partial patch happens to pass. Keep its correctness diagnostic.
            if agent.timed_out:
                result.update(
                    status="timeout",
                    error=(
                        f"Agent exceeded its time limit ({config.timeout:g} s; --timeout)"
                        if config.timeout is not None
                        else "Agent execution reported a timeout despite --timeout unlimited; inspect execution logs"
                    ),
                    score=0,
                )
            elif agent.output_limited:
                result.update(
                    status="error",
                    error=_output_limit_error("Agent", config.max_log_bytes),
                    score=0,
                )
            elif agent.returncode or observed_error:
                result.update(
                    status="error",
                    error=observed_error
                    or (
                        f"Agent exited with code {agent.returncode} without an explicit harness "
                        "error; inspect agent stdout/stderr"
                    ),
                    score=0,
                )
    except KeyboardInterrupt:
        result.update(status="interrupted", score=0, error="Attempt interrupted by user")
    except ContainerCleanupError as exc:
        result.update(status="error", score=0, error=str(exc), abort_run=True)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result.update(status="error", score=0, error=str(exc))
    finally:
        result.update(duration_seconds=time.monotonic() - started, finished_at=utc_now())
        result["outcome_reason"] = describe_outcome(result)
    return _redact(result, secret)


def task_manifest(tasks: list[Task]) -> list[dict]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "category": t.category,
            "prompt": t.prompt,
            "metadata": t.metadata,
            "fixture_sha256": hashlib.sha256(
                json.dumps(t.files, sort_keys=True).encode()
            ).hexdigest(),
            "hidden_tests_sha256": hashlib.sha256(t.hidden_tests.encode()).hexdigest(),
        }
        for t in tasks
    ]


def run_benchmark(config: RunConfig) -> tuple[dict, Path]:
    config.validate()
    tasks = config.selected_tasks()
    task_sets = config.selected_task_sets()
    set_manifest = [
        {
            "id": task_set.id,
            "title": task_set.title,
            "description": task_set.description,
            "task_ids": list(task_set.task_ids),
        }
        for task_set in task_sets
    ]
    membership = {
        task.id: [task_set.id for task_set in task_sets if task.id in task_set.task_ids]
        for task in tasks
    }
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = Path(config.output).expanduser().resolve() / run_id
    output.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "framework_version": __version__,
        "run_id": run_id,
        "created_at": utc_now(),
        "finished_at": None,
        "status": "running",
        "model": {
            "id": config.model,
            "base_url": config.base_url,
            "context_window": config.context_window,
            "max_tokens": config.max_tokens,
            "reasoning": config.reasoning,
            "thinking": config.thinking,
            "api_profile": config.api_profile,
        },
        "parameters": asdict(config),
        "environment": {
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "credential_present": bool(os.environ.get(config.api_key_env)),
        },
        "task_manifest": task_manifest(tasks),
        "task_set_manifest": set_manifest,
        "resolved_tasks": [task.id for task in tasks],
        "results": [],
        "summary": summarize([], set_manifest),
        "methodology": {
            "score": "100 × hidden unittest methods passed / total; each task equally weighted",
            "execution_errors": "Timeouts and harness/infrastructure errors count as 0, with partial grading retained",
            "repeats": "Fresh independent workspaces, serial execution, seeded shuffled attempt order; not pass@k",
            "task_sets": "Selected sets expand in order, followed by explicit tasks; overlapping tasks run once per harness/repeat and appear in each applicable set breakdown, without double-counting overall metrics",
            "durations": "Host wall time includes agent startup, grading and cleanup; separate agent/grader times recorded",
            "usage": "Primary harness event stream only; unknown counts are null; background calls may be omitted",
            "termination": "Final emitted response only; length is a diagnostic warning, not an execution error; grading and status are unchanged",
            "limits": "Docker bounds client CPU/RAM/time, not remote server GPU/RAM or total tokens",
            "scope": "Synthetic Python tasks, not a production-repository or contamination-resistant benchmark",
            "submission": "Only regular UTF-8 solution/**/*.py files are graded; tests/configs are never imported on host",
        },
    }
    docker = Docker(config)
    secret = os.environ.get(config.api_key_env)

    def persist():
        report["summary"] = summarize(report["results"], set_manifest)
        write_reports(_redact(report, secret), output)

    persist()
    try:
        if config.timeout is None:
            print(
                "WARNING: Agent timeout is unlimited; an attempt may never finish. "
                "Use Ctrl+C for normal interruption and container cleanup.",
                flush=True,
            )
        if config.max_log_bytes is None:
            print(
                "WARNING: Log output is unlimited; host temporary disk, report size, and "
                "host RAM usage are not bounded by Docker resource limits.",
                flush=True,
            )
        report["environment"]["docker"] = docker.available()
        if config.build:
            docker.build_images()
        base = docker.image_metadata(BASE_IMAGE)
        images = {
            h: docker.image_metadata(image_tag(h, config.harness_versions[h]))
            for h in config.harnesses
        }
        report["environment"].update(base_image=base, harness_images=images)
        # Execute immutable IDs, not mutable tags, throughout this benchmark.
        schedule = [
            (h, t, r) for r in range(1, config.repeats + 1) for t in tasks for h in config.harnesses
        ]
        random.Random(config.seed).shuffle(schedule)
        report["schedule"] = [
            {"harness": h, "task_id": t.id, "repeat": r, "task_sets": membership[t.id]}
            for h, t, r in schedule
        ]
        persist()
        for index, (harness, task, repeat) in enumerate(schedule, 1):
            print(
                f"[{index}/{len(schedule)}] {config.model} · {harness} · {task.id} · repeat {repeat}",
                flush=True,
            )
            result = run_attempt(
                docker, config, task, harness, repeat, images[harness]["id"], base["id"]
            )
            result["task_sets"] = membership[task.id]
            report["results"].append(result)
            print(
                f"  {result['status']}: {result['score']:.2f}/100 in {result['duration_seconds']:.1f}s",
                flush=True,
            )
            if result["status"] != "success":
                print(
                    f"  Outcome: {result.get('outcome_reason') or describe_outcome(result)}",
                    flush=True,
                )
            termination = result.get("termination") or {}
            if any(
                termination.get(field) is not None
                for field in ("reason", "output_tokens", "reasoning_tokens")
            ):
                details = ", ".join(
                    f"{label}={termination.get(field) if termination.get(field) is not None else 'n/a'}"
                    for field, label in (
                        ("reason", "reason"),
                        ("output_tokens", "output tokens"),
                        ("reasoning_tokens", "reasoning tokens"),
                    )
                )
                print(f"  Final response: {details}", flush=True)
            if termination.get("warning"):
                print(f"  WARNING: {termination['warning']}", flush=True)
            persist()
            if result["status"] == "interrupted":
                report.update(
                    status="interrupted", error="Interrupted by user; started attempts are retained"
                )
                break
            if result.get("abort_run"):
                report.update(status="error", error=result.get("error"))
                break
        else:
            report["status"] = "completed"
    except KeyboardInterrupt:
        report.update(
            status="interrupted", error="Interrupted by user; completed attempts are retained"
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        report.update(status="error", error=str(exc))
    finally:
        report["finished_at"] = utc_now()
        persist()
    return _redact(report, secret), output


def probe_endpoint(base_url: str, model: str | None, api_key_env: str, timeout: float = 10) -> dict:
    """Diagnostic metadata only; does not send prompts or evaluate a model."""
    base_url = normalize_base_url(base_url)
    headers = {"Accept": "application/json"}
    key = os.environ.get(api_key_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(base_url + "/models", headers=headers)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("/models response exceeded 1 MB")
    data = json.loads(raw)
    ids = [
        item["id"]
        for item in data.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    return {
        "base_url": base_url,
        "models": ids,
        "requested_model": model,
        "model_present": model in ids if model else None,
        "note": "Host reachability only; Docker routing and tool calling must also work.",
    }

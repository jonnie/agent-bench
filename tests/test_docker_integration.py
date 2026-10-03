"""Opt-in real-container tests; no LLM, credentials, pulls, or builds required.

Prebuild agent-bench-base:local and the pinned HARNESS_VERSIONS image tags, then:
    PYTHONPATH=src AGENT_BENCH_DOCKER_TESTS=1 python3 -m unittest discover \
        -s tests -p test_docker_integration.py

The host HTTP fixture exercises OpenAI-compatible streaming and native tools,
not inference quality or compatibility with a real llama.cpp server.
"""

import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from agent_bench import runner
from agent_bench.harnesses import HARNESS_VERSIONS, image_tag, parse_usage, prepare_harness
from agent_bench.task_sets import TASK_SETS, TaskSet
from agent_bench.tasks import TASKS

# Candidate source stays data on the host; only the harness container executes it.
BILLING_SOURCE = """from decimal import Decimal, ROUND_HALF_EVEN


def invoice_total(items):
    total = sum(
        (Decimal(item['quantity']) * Decimal(item['unit_price'])
         * (Decimal(1) - Decimal(item.get('discount_pct', '0')) / Decimal(100))
         for item in items),
        Decimal(0),
    )
    rounded = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
    return format(rounded, '.2f') if rounded else '0.00'
"""
TOOL_MARKER = "AGENT_BENCH_MOCK_PATCH_APPLIED"
# OMP's native persistent shell accepts a single command, not heredocs/scripts.
# A quoted Python invocation works for all three advertised bash tools.
BASH_COMMAND = "python -c " + shlex.quote(
    "from pathlib import Path; import subprocess; "
    "private = Path('/workspace/scratch/private'); "
    "private.mkdir(mode=0o700, parents=True); private.chmod(0o700); "
    "readonly = private / 'readonly.txt'; "
    "readonly.write_text('cleanup must remove private artifacts', encoding='utf-8'); "
    "readonly.chmod(0o400); "
    f"Path('/workspace/solution/billing.py').write_text({BILLING_SOURCE!r}, encoding='utf-8'); "
    "subprocess.run(['python', '-m', 'unittest', 'discover', '-s', 'tests'], "
    "check=True, timeout=30); "
    f"print({TOOL_MARKER!r})"
)
FINAL_TEXT = "Fixed invoice boundary rounding, refunds, and negative zero; public tests pass."
USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class MockModelServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        super().__init__(("0.0.0.0", 0), MockModelHandler)
        self.records = []
        self.errors = []
        self.lock = threading.Lock()


class MockModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def send_json(self, status, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "served-model", "object": "model", "owned_by": "integration-test"},
                    ],
                },
            )
        else:
            self.send_json(404, {"error": {"message": "Unexpected mock endpoint: " + self.path}})

    def do_POST(self):
        server = cast(MockModelServer, self.server)
        try:
            if self.path != "/v1/chat/completions":
                raise ValueError("Unexpected mock endpoint: " + self.path)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("Missing or oversized request body")
            body = json.loads(self.rfile.read(length))
            if body.get("model") != "served-model":
                raise ValueError("Unexpected model: " + repr(body.get("model")))
            messages = body.get("messages", [])
            functions = [
                tool["function"] for tool in body.get("tools", []) if tool.get("type") == "function"
            ]
            record = {
                "path": self.path,
                "model": body["model"],
                "stream": body.get("stream"),
                "chat_template_kwargs": body.get("chat_template_kwargs"),
                "max_tokens": body.get("max_tokens"),
                "max_completion_tokens": body.get("max_completion_tokens"),
                "reasoning_effort": body.get("reasoning_effort"),
                "roles": [message.get("role") for message in messages],
                "last_message": messages[-1] if messages else None,
            }
            with server.lock:
                server.records.append(record)
                number = len(server.records)
            tool_result = bool(messages and messages[-1].get("role") == "tool")
            if tool_result:
                record["phase"] = "final"
                delta = {"content": FINAL_TEXT}
                finish_reason = "stop"
            elif not functions:
                # OpenCode also requests a session title from its configured
                # small model. Keep that auxiliary call on the same local mock.
                record["phase"] = "auxiliary"
                delta = {"content": "Fix invoice precision"}
                finish_reason = "stop"
            else:
                function = next((item for item in functions if item.get("name") == "bash"), None)
                if function is None:
                    raise ValueError(
                        "No native bash tool advertised: "
                        + repr([item.get("name") for item in functions])
                    )
                record["bash_schema"] = function.get("parameters", {})
                properties = function.get("parameters", {}).get("properties", {})
                if "command" not in properties:
                    raise ValueError("Unrecognized bash schema: " + json.dumps(function))
                arguments = {"command": BASH_COMMAND}
                intent = "Fix Decimal invoice rounding and run public tests"
                if "description" in properties:
                    arguments["description"] = intent
                if "i" in properties:
                    arguments["i"] = intent
                required = set(function.get("parameters", {}).get("required", []))
                if required - arguments.keys():
                    raise ValueError(
                        "Unhandled required bash fields: " + repr(required - arguments.keys())
                    )
                record.update(phase="tool_call", arguments=arguments)
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_billing_{number}",
                            "type": "function",
                            "function": {
                                "name": function["name"],
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
                finish_reason = "tool_calls"
            if body.get("stream") is not True:
                raise ValueError("Expected a streaming chat completion")
            self.send_stream(number, delta, finish_reason)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with server.lock:
                server.errors.append(str(exc))
            # If a client disconnects, preserve the diagnostic without retrying a write.
            if not isinstance(exc, OSError):
                self.send_json(
                    400, {"error": {"message": str(exc), "type": "invalid_request_error"}}
                )

    def send_stream(self, number, delta, finish_reason):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        metadata = {
            "id": f"chatcmpl-test-{number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "served-model",
        }
        # Separate role, payload, finish, and usage events exercise SSE parsing.
        for choice in (
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None},
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": finish_reason},
        ):
            self.write_event({**metadata, "choices": [choice]})
        self.write_event({**metadata, "choices": [], "usage": USAGE})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def write_event(self, event):
        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
        self.wfile.flush()


@unittest.skipUnless(
    os.environ.get("AGENT_BENCH_DOCKER_TESTS") == "1",
    "Set AGENT_BENCH_DOCKER_TESTS=1 to run prebuilt Docker integration tests",
)
class DockerOwnershipIntegrationTests(unittest.TestCase):
    def test_unlimited_agent_limits_capture_complete_output_and_cleanup(self):
        docker = runner.Docker(
            runner.RunConfig(model="unused", build=False, timeout=None, max_log_bytes=None)
        )
        docker.available()
        image = docker.image_metadata(runner.BASE_IMAGE)["id"]
        result = docker.execute(
            image=image,
            mounts=[],
            command=[
                "python",
                "-c",
                "import sys,time; time.sleep(0.1); sys.stdout.write('x'*2100000); sys.stderr.write('y'*2100000)",
            ],
            timeout=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_limited)
        self.assertEqual(result.stdout, "x" * 2_100_000)
        self.assertEqual(result.stderr, "y" * 2_100_000)

    def test_linux_volume_cleanup_requires_matching_agent_uid(self):
        # Named volumes use the daemon's Linux filesystem, even on Docker Desktop.
        # No host bind mounts, harness images, or model endpoint are needed here.
        docker = runner.Docker(runner.RunConfig(model="unused", build=False))
        docker.available()
        identifiers = docker.checked(
            [
                "image",
                "ls",
                "--no-trunc",
                "--filter",
                "reference=" + runner.BASE_IMAGE,
                "--format",
                "{{.ID}}",
            ],
            timeout=30,
        ).stdout.splitlines()
        self.assertEqual(len(identifiers), 1, f"Missing prebuilt base image: {identifiers}")
        image = docker.image_metadata(identifiers[0])["id"]
        volume = "agent-bench-ownership-" + uuid.uuid4().hex

        def run_as(uid, script, *arguments):
            name = volume + "-" + uuid.uuid4().hex[:8]
            command = [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                name,
                *docker.limits(),
                "--network",
                "none",
                "--read-only",
                "--user",
                f"{uid}:{uid}",
                "--mount",
                f"type=volume,source={volume},target=/workspace,volume-nocopy",
                "--workdir",
                "/workspace",
                image,
                "python",
                "-c",
                script,
                *arguments,
            ]
            try:
                result = runner.run_command(command, timeout=30, max_output=16384)
                diagnostics = (
                    f"UID {uid}, arguments {arguments}:\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
                self.assertFalse(result.timed_out, diagnostics)
                self.assertFalse(result.output_limited, diagnostics)
                self.assertEqual(result.returncode, 0, diagnostics)
                return result.stdout
            finally:
                # --rm alone cannot clean up after a timed-out Docker client.
                docker.remove(name)

        try:
            docker.checked(["volume", "create", volume], timeout=30)
            # The only root execution initializes this empty, ephemeral volume.
            # CAP_DAC_OVERRIDE/CHOWN are not needed: root owns the volume root.
            run_as(
                0,
                """
import os
from pathlib import Path
root = Path('/workspace')
assert os.getuid() == os.getgid() == 0
assert root.stat().st_uid == root.stat().st_gid == 0
assert not list(root.iterdir())
root.chmod(0o777)
assert root.stat().st_mode & 0o777 == 0o777
""",
            )
            run_as(
                1001,
                """
import os
from pathlib import Path
assert os.getuid() == os.getgid() == 1001
for name in ('before', 'after'):
    root = Path('/workspace') / name
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    assert root.stat().st_uid == root.stat().st_gid == 1001
    (root / 'ownership_fixture.py').write_text('VALUE = 42\\n', encoding='utf-8')
""",
            )
            writer = """
import os
import sys
from pathlib import Path
uid = int(sys.argv[2])
assert os.getuid() == os.getgid() == uid
root = Path('/workspace') / sys.argv[1]
sys.dont_write_bytecode = False
sys.path.insert(0, str(root))
import ownership_fixture
assert ownership_fixture.VALUE == 42
cache = root / '__pycache__'
bytecode = list(cache.glob('ownership_fixture.*.pyc'))
assert len(bytecode) == 1, list(cache.iterdir())
private = root / 'private'
private.mkdir(mode=0o700)
private.chmod(0o700)
readonly = private / 'readonly.txt'
readonly.write_text('private artifact', encoding='utf-8')
readonly.chmod(0o400)
assert private.stat().st_mode & 0o777 == 0o700
assert readonly.stat().st_mode & 0o777 == 0o400
for path in (cache, bytecode[0], private, readonly):
    info = path.stat()
    assert info.st_uid == info.st_gid == uid, (str(path), info)
    print(f'{path}: uid={info.st_uid} gid={info.st_gid} mode={info.st_mode & 0o777:o}')
"""
            run_as(1000, writer, "before", "1000")
            failures = run_as(
                1001,
                """
import os
import shutil
from pathlib import Path
assert os.getuid() == os.getgid() == 1001
root = Path('/workspace/before')
for path in (root / '__pycache__', root / 'private', root):
    try:
        shutil.rmtree(path)
    except PermissionError as error:
        print(f'EXPECTED_PERMISSION_ERROR: {path}: {error}')
    else:
        raise AssertionError(f'UID 1001 unexpectedly removed UID 1000 artifacts: {path}')
    assert path.exists(), path
""",
            )
            self.assertEqual(failures.count("EXPECTED_PERMISSION_ERROR:"), 3, failures)
            run_as(1001, writer, "after", "1001")
            success = run_as(
                1001,
                """
import os
import shutil
from pathlib import Path
assert os.getuid() == os.getgid() == 1001
root = Path('/workspace/after')
shutil.rmtree(root)
assert not root.exists()
assert Path('/workspace/before').exists()
print('MATCHING_UID_CLEANUP_SUCCEEDED')
""",
            )
            self.assertIn("MATCHING_UID_CLEANUP_SUCCEEDED", success, success)
        finally:
            # Docker removes even the intentionally undeletable UID 1000 tree;
            # no additional root container is allowed for cleanup.
            docker.checked(["volume", "rm", "--force", volume], timeout=30)


@unittest.skipUnless(
    os.environ.get("AGENT_BENCH_DOCKER_TESTS") == "1",
    "Set AGENT_BENCH_DOCKER_TESTS=1 to run prebuilt Docker integration tests",
)
class DockerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Once opted in, missing Docker/images are failures, not silent skips.
        cls.docker = runner.Docker(runner.RunConfig(model="served-model", build=False))
        cls.docker.available()
        # Resolve the baseline fixture by ID independently of the runner's tag
        # inspection, so an orchestration failure cannot suppress grader coverage.

        tags = [runner.BASE_IMAGE, *(image_tag(h, v) for h, v in HARNESS_VERSIONS.items())]
        identifiers = {}
        for tag in tags:
            listing = cls.docker.checked(
                ["image", "ls", "--no-trunc", "--filter", "reference=" + tag, "--format", "{{.ID}}"]
            ).stdout.splitlines()
            if len(listing) != 1:
                raise RuntimeError(f"Expected one prebuilt image for {tag}, found {listing}")
            identifiers[tag] = listing[0]
        cls.base_image = cls.docker.image_metadata(identifiers[runner.BASE_IMAGE])["id"]

    def test_all_native_harnesses_patch_and_pass_hidden_grading(self):
        server = MockModelServer()
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="agent-bench-docker-results-") as directory:
                config = runner.RunConfig(
                    model="served-model",
                    base_url=f"http://host.docker.internal:{server.server_port}/v1",
                    task_sets=["integration"],
                    tasks=["bug-fix"],
                    harnesses=list(HARNESS_VERSIONS),
                    repeats=1,
                    build=False,
                    timeout=90,
                    output=directory,
                )
                # No real credential is needed or forwarded, even on a configured host.
                with (
                    patch.dict(os.environ),
                    patch.dict(
                        TASK_SETS,
                        {
                            "integration": TaskSet(
                                "integration",
                                "Integration test",
                                "One task through all native tools.",
                                ("bug-fix",),
                            ),
                        },
                    ),
                ):
                    os.environ.pop(config.api_key_env, None)
                    report, output = runner.run_benchmark(config)
                diagnostics = json.dumps(
                    {
                        "results": report["results"],
                        "error": report.get("error"),
                        "summary": report.get("summary"),
                        "mock_errors": server.errors,
                        "requests": server.records,
                    },
                    indent=2,
                )
                self.assertEqual(report["status"], "completed", diagnostics)
                self.assertFalse(report["environment"]["credential_present"])
                self.assertEqual(len(report["results"]), len(HARNESS_VERSIONS), diagnostics)
                self.assertEqual(
                    {item["harness"] for item in report["results"]}, set(HARNESS_VERSIONS)
                )
                for result in report["results"]:
                    with self.subTest(harness=result["harness"]):
                        self.assertEqual(result["status"], "success", diagnostics)
                        self.assertEqual(result["score"], 100, diagnostics)
                        self.assertEqual(result["task_sets"], ["integration"])
                        self.assertEqual(result["agent_exit_code"], 0, diagnostics)
                        self.assertEqual(result["grader_exit_code"], 0, diagnostics)
                        grading = runner.validate_grade(result["grading"])
                        self.assertTrue(grading["success"], diagnostics)
                        self.assertEqual(grading["tests_total"], 5)
                        self.assertEqual(grading["tests_failed"] + grading["tests_errors"], 0)
                        self.assertIn("solution/billing.py", result["patch"]["diff"])
                        self.assertIn(FINAL_TEXT, result["logs"]["stdout"], diagnostics)
                        self.assertGreaterEqual(result["usage"]["tool_calls"], 1)
                        self.assertGreater(result["usage"]["input_tokens"], 0)
                        self.assertGreater(result["usage"]["output_tokens"], 0)
                self.assertEqual(report["summary"]["successes"], 3, diagnostics)
                self.assertEqual(report["summary"]["success_rate"], 1, diagnostics)
                self.assertEqual(report["resolved_tasks"], ["bug-fix"])
                self.assertEqual(report["task_set_manifest"][0]["id"], "integration")
                self.assertEqual(
                    report["summary"]["per_task_set"]["integration"]["successes"], 3, diagnostics
                )
                self.assertEqual(output.parent, Path(directory).resolve())
                self.assertEqual(list(Path(directory).iterdir()), [output])
                self.assertEqual(
                    {path.relative_to(output).as_posix() for path in output.rglob("*")},
                    {"results.json", "results.html"},
                )
                self.assertEqual(
                    json.loads((output / "results.json").read_text(encoding="utf-8")), report
                )
                html = (output / "results.html").read_text(encoding="utf-8")
                self.assertIn("<html", html)
                for harness in HARNESS_VERSIONS:
                    self.assertIn(harness, html)
                self.assertEqual(server.errors, [], diagnostics)
                calls = [record for record in server.records if record.get("phase") == "tool_call"]
                finals = [record for record in server.records if record.get("phase") == "final"]
                self.assertEqual(len(calls), 3, diagnostics)
                self.assertEqual(len(finals), 3, diagnostics)
                for record in finals:
                    self.assertIn(TOOL_MARKER, json.dumps(record["last_message"]), diagnostics)
                for record in server.records:
                    self.assertEqual(
                        record["chat_template_kwargs"], {"enable_thinking": False}, diagnostics
                    )
                    self.assertEqual(
                        record["max_tokens"] or record["max_completion_tokens"],
                        config.max_tokens,
                        diagnostics,
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "Mock model server did not stop")

    def test_native_api_profiles_control_every_request_without_changing_usage(self):
        # Direct adapter calls keep profile coverage independent of the CLI/runner.
        scenarios = (
            ("llama-cpp", False, "off"),
            ("llama-cpp", True, "off"),
            ("llama-cpp", True, "low"),
            ("openai-compatible", False, "off"),
            ("openai-compatible", True, "low"),
        )
        for harness, version in HARNESS_VERSIONS.items():
            expected_usage = None
            for profile, reasoning, thinking in scenarios:
                with self.subTest(
                    harness=harness, profile=profile, reasoning=reasoning, thinking=thinking
                ):
                    server = MockModelServer()
                    thread = threading.Thread(
                        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
                    )
                    thread.start()
                    try:
                        with tempfile.TemporaryDirectory(
                            prefix="agent-bench-payload-"
                        ) as directory:
                            root = Path(directory)
                            workspace = root / "workspace"
                            workspace.mkdir(mode=0o777)
                            task = TASKS["bug-fix"]
                            for relative, contents in task.files.items():
                                target = workspace / relative
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_text(contents, encoding="utf-8")
                                target.chmod(0o666)
                            for current, _, _ in os.walk(workspace):
                                Path(current).chmod(0o777)
                            config = {
                                "model": "served-model",
                                "base_url": f"http://host.docker.internal:{server.server_port}/v1",
                                "context_window": 32768,
                                "max_tokens": 1731,
                                "api_profile": profile,
                                "reasoning": reasoning,
                                "thinking": thinking,
                            }
                            with patch.dict(os.environ):
                                os.environ.pop("AGENT_BENCH_API_KEY", None)
                                adapter = prepare_harness(harness, config, root / "config")
                            agent = self.docker.execute(
                                image=image_tag(harness, version),
                                mounts=[(workspace, "/workspace", False)]
                                + [(source, target, True) for source, target in adapter["mounts"]],
                                command=[*adapter["command"], task.prompt],
                                environment=adapter["environment"],
                                timeout=90,
                            )
                            diagnostics = json.dumps(
                                {
                                    "stdout": agent.stdout,
                                    "stderr": agent.stderr,
                                    "requests": server.records,
                                    "mock_errors": server.errors,
                                },
                                indent=2,
                            )
                            self.assertEqual(agent.returncode, 0, diagnostics)
                            self.assertEqual(server.errors, [], diagnostics)
                            self.assertIn(FINAL_TEXT, agent.stdout, diagnostics)
                            self.assertEqual(
                                (workspace / "solution/billing.py").read_text(), BILLING_SOURCE
                            )
                            phases = [record["phase"] for record in server.records]
                            self.assertEqual(phases.count("tool_call"), 1, diagnostics)
                            self.assertEqual(phases.count("final"), 1, diagnostics)
                            if harness == "opencode":
                                self.assertIn("auxiliary", phases, diagnostics)
                            for record in server.records:
                                self.assertEqual(record["model"], config["model"], diagnostics)
                                self.assertEqual(
                                    record["chat_template_kwargs"],
                                    {"enable_thinking": thinking != "off"}
                                    if profile == "llama-cpp"
                                    else None,
                                    diagnostics,
                                )
                                limits = [
                                    record[field]
                                    for field in ("max_tokens", "max_completion_tokens")
                                    if record[field] is not None
                                ]
                                self.assertEqual(limits, [config["max_tokens"]], diagnostics)
                                if harness == "opencode":
                                    effort = None
                                    if profile == "openai-compatible" and reasoning:
                                        effort = thinking
                                    elif reasoning and record["phase"] == "auxiliary":
                                        # Native smallOptions adds low effort; model options
                                        # still enforce template thinking and the token cap.
                                        effort = "low"
                                    self.assertEqual(
                                        record["reasoning_effort"], effort, diagnostics
                                    )
                            usage = parse_usage(harness, agent.stdout)
                            self.assertEqual(usage["input_tokens"], 200, diagnostics)
                            self.assertEqual(usage["output_tokens"], 40, diagnostics)
                            if expected_usage is None:
                                expected_usage = usage
                            self.assertEqual(usage, expected_usage, diagnostics)
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=5)
                    self.assertFalse(thread.is_alive(), "Mock model server did not stop")

    def test_every_baseline_fails_in_clean_network_disabled_grader(self):
        intended_failures = {
            "bug-fix": {"test_fractional_cent_accumulation", "test_half_even_and_zero"},
            "small-feature": {
                "test_nested_delete_and_type_changes",
                "test_lists_replace_and_all_mutables_are_detached",
            },
            "system-refactor": {
                "test_canonical_policy",
                "test_architecture_not_just_behavior",
                "test_dynamic_delegation",
            },
            "security-hardening": {
                "test_symlinks_inside_outside_and_dangling",
                "test_open_is_directory_anchored_and_race_safe",
            },
            "performance": {"test_linear_work_and_generous_runtime"},
        }
        self.assertEqual(set(intended_failures), set(TASKS))
        for task in TASKS.values():
            with (
                self.subTest(task=task.id),
                tempfile.TemporaryDirectory(prefix="agent-bench-docker-baseline-") as directory,
            ):
                root = Path(directory)
                candidate, grader = root / "candidate", root / "grader"
                candidate.mkdir()
                grader.mkdir()
                for relative, contents in task.files.items():
                    if relative.startswith("solution/") and relative.endswith(".py"):
                        destination = candidate / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(contents, encoding="utf-8")
                shutil.copyfile(PROJECT / "src/agent_bench/grader.py", grader / "grader.py")
                (grader / "hidden_tests.py").write_text(task.hidden_tests, encoding="utf-8")
                result = self.docker.execute(
                    image=self.base_image,
                    mounts=[(candidate, "/candidate", True), (grader, "/grader", True)],
                    command=[
                        "python",
                        "-I",
                        "-B",
                        "/grader/grader.py",
                        "/candidate",
                        "/grader/hidden_tests.py",
                    ],
                    timeout=60,
                    grade=True,
                )
                self.assertFalse(result.timed_out, result)
                self.assertFalse(result.output_limited, result)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                grading = runner.validate_grade(json.loads(result.stdout))
                self.assertFalse(grading["success"], grading)
                self.assertLess(grading["score"], 100)
                self.assertGreater(grading["tests_passed"], 0, grading)
                failures = {
                    case["name"].rsplit(".", 1)[-1]
                    for case in grading["cases"]
                    if case["status"] != "passed"
                }
                self.assertTrue(intended_failures[task.id] <= failures, grading)
                self.assertNotIn("load_or_run", failures, grading)
                print(
                    f"Baseline {task.id}: {grading['tests_passed']}/{grading['tests_total']} passed; "
                    f"intended failures: {', '.join(sorted(failures))}",
                    flush=True,
                )


if __name__ == "__main__":
    unittest.main()

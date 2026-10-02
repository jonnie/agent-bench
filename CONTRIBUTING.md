# Contributing to Agent Bench

Thanks for helping improve this experimental benchmarking framework. Discussions, bug reports, documentation improvements, tasks, and harness integrations are welcome.

## Before opening an issue

Search [existing issues](https://github.com/jonnie/agent-bench/issues). Report your framework/Python/Docker versions, host architecture, harness versions, command (without secrets), expected behavior, and a minimal reproduction. Distinguish an incorrect model-generated solution from a framework/harness integration failure. Do not upload raw transcripts or private endpoint URLs without reviewing them. Report vulnerabilities using [SECURITY.md](SECURITY.md), not public issues.

## Development setup

Python 3.11+ is required. Docker is needed only for container checks and actual benchmarks.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pyright src tests scripts
```

Ordinary tests do not require a model server. Installing the `dev` extra enables JSON Schema validation; only the opt-in Docker integration tests should skip in the ordinary suite. Some trusted fixture/reference tests execute Python in host subprocesses; never substitute unreviewed or model-generated code into those fixtures.

For container/tool integration, build the pinned images and run the local mock API tests:

```sh
.venv/bin/agent-bench build
AGENT_BENCH_DOCKER_TESTS=1 .venv/bin/python -m unittest discover \
  -s tests -p test_docker_integration.py -v
```

Image builds need network access to upstream registries and package repositories. The tests use a mock model, not LAN inference or paid APIs. Use a disposable machine/VM for untrusted code; Docker is not a complete hostile-code security boundary.

## Changes and pull requests

- Keep changes focused and describe their user-visible impact and validation.
- Add regression tests for fixes, CLI changes, and reporting changes.
- Update documentation and [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
- Preserve host/container isolation, credential handling, cleanup, and independent grading.
- Do not commit local `results/`, virtual environments, build products, API keys, or sensitive transcripts.
- Do not change acceptance criteria merely to make a particular model score better. Explain and validate any correction to a task contract or grader.
- By contributing, you agree to license your contribution under the project's MIT license and confirm you have the right to contribute it. Third-party code/data must have a compatible license and appropriate attribution.

## Tasks and task sets

See [the extension guide in README.md](README.md#adding-tasks-and-task-sets). Each task exports `TASK` in `src/agent_bench/tasks/`; each set exports `TASK_SET` in `src/agent_bench/task_sets/`. Both have explicit registries.

Use stable, unique IDs and precise public contracts. Add baseline and correct-reference grading tests, test mutation/edge cases, and keep hidden tests out of agent containers. Explicitly choose set membership; registering a task does not silently broaden the default suite. Update preservation hashes only when the fixture change is intentional and reviewed.

## Report compatibility and publishing

[The report contract](docs/REPORT_FORMAT.md) describes schema version 1. Additive optional fields may remain version 1; incompatible changes require an explicit version/migration plan. Update the bundled schema and contract tests together. Re-render old reports using recorded manifests, not the current task registry.

The [checked-in example](examples/README.md) is reviewed, sanitized measured data. Its scrubber is specific to that historical source, not a general publication exporter. Review both JSON and HTML before sharing any new results.

## Distribution checks

```sh
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/check_distribution.py dist/*.whl dist/*.tar.gz
```

The checker installs each artifact into a separate clean environment and checks resources, CLI selection, and report rendering without importing the checkout. It may download sdist build dependencies. Never run it on an untrusted source distribution: its build backend executes code.

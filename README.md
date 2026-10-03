# Agent Bench

**Experimental alpha · Python 3.11+ · MIT licensed**

[Source](https://github.com/jonnie/agent-bench) · [Issues](https://github.com/jonnie/agent-bench/issues) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Report format](docs/REPORT_FORMAT.md) · [Example results](examples/README.md) · [Changelog](CHANGELOG.md)

Compare **one LAN-hosted LLM** across **pi**, **oh-my-pi**, and **OpenCode**. A Python CLI orchestrates fresh Docker containers on the host, grades the resulting patches independently, and writes exactly **one JSON file and one standalone HTML file per run**.

Works with llama.cpp's OpenAI-compatible `/v1/chat/completions` API, including streamed tool calls. The host needs Python 3.11+ and Docker Desktop/Engine; it does not need Node, Bun, or any harness installed. The Python CLI has no runtime package dependencies.

## Quick start

From a checkout of [jonnie/agent-bench](https://github.com/jonnie/agent-bench):

```sh
git clone https://github.com/jonnie/agent-bench.git
cd agent-bench
python3 -m venv .venv
.venv/bin/python -m pip install -e .

.venv/bin/agent-bench list
.venv/bin/agent-bench build
.venv/bin/agent-bench doctor \
  --base-url http://192.168.1.50:8080 \
  --model qwen-coder

.venv/bin/agent-bench run \
  --base-url http://192.168.1.50:8080 \
  --model qwen-coder \
  --no-build
```

Replace the URL with the address of **the other computer** and the model ID with the exact ID advertised by `/v1/models`. `--llama-url` is an alias for `--base-url`. A bare server URL is normalized to `/v1`; URLs already ending in `/v1` are retained. Do not supply the `/chat/completions` URL.

Default runs execute the `default` task set (the existing five tasks) through all three harnesses, one fresh attempt each, **serially** in a seeded shuffled order. Serial execution avoids competition for your remote model server. `run` builds missing/current images by default; `--no-build` requires prebuilt images and skips builds entirely.

Results are printed as the run progresses:

```text
results/20261001T120000Z-a1b2c3d4/
  results.json
  results.html
```

Open `results.html` directly in a browser. It works offline with no HTTP server, JavaScript, CDNs, or external assets. Reports show the single model at the top, then harness comparisons, selected task sets and their scores, task scores, individual checks, errors, logs, prompts, parameters, and production diffs. JSON content is also duplicated in the HTML. Both files are refreshed after each attempt so partial results survive normal interruption.

See the [sanitized measured example](examples/README.md) for a complete 45-attempt report. It retains failures, but intentionally omits private transcripts/endpoints and does not establish the exact model weights or server deployment. Download/open its HTML locally; GitHub's file viewer does not render it as a website.

Attempt details separate **Outcome** from **Execution error**. A `failed` attempt with no execution error means the generated code failed hidden checks; the outcome names the failing checks and summarizes their assertions. Timeouts can retain a passing diagnostic grade, but still receive an attempt score of zero because the agent did not finish. Historical reports gain these explanations when re-rendered, without changing their recorded measurements.

The JSON is the source of truth for website tables/charts and supporting article data. [Schema version 1](docs/REPORT_FORMAT.md) is documented and bundled with the package. The CLI report renderer checks the envelope, not the full schema; full schema validation is a development/publication check. Do not publish raw run transcripts without review or let an LLM invent numerical results.

### Set up llama.cpp on the other machine

For example, on your **model server**, using an appropriate GGUF and a current llama.cpp build:

```sh
llama-server \
  --model /path/to/model.gguf \
  --alias qwen-coder \
  --host 0.0.0.0 \
  --port 8080 \
  --ctx-size 32768 \
  --jinja
```

The model and chat template must support **tool calling**. Consult your llama.cpp/model documentation for any required template or reasoning settings; an endpoint that only generates prose is not sufficient. The framework configures all three harnesses with the same custom OpenAI-compatible provider and the exact selected model; it does not pull or serve weights.

- Match `--context-window` to the server's configured usable context, not just the model's advertised maximum.
- Default context metadata is 32,768 tokens and maximum response length is 4,096 tokens. `--max-tokens` is a per-response setting, **not a total attempt token budget**.
- The default `--api-profile llama-cpp` explicitly sends `chat_template_kwargs.enable_thinking=false` for `--thinking off` across all three harnesses. Native harness thinking settings alone do not disable a server's reasoning chat template; otherwise a response can spend its entire token budget thinking without editing files. Non-off thinking levels send `enable_thinking=true`; the model/template must support this flag. Use `--api-profile openai-compatible` for other endpoints that should not receive llama.cpp template controls. The profile and effective request controls are recorded in reports.
- GPU offload, quantization, sampling/temperature, server-side seed, prompt caching, and llama.cpp version are controlled on the server. Keep these constant and record them with your experiment notes; the CLI scheduling seed does not make model sampling deterministic.
- Open the server port to the benchmark machine/Docker network. Use only a trusted LAN or an authenticated tunnel; binding to `0.0.0.0` is not a security boundary.
- API authentication is optional. If enabled, set `AGENT_BENCH_API_KEY` in your host environment or use `--api-key-env OTHER_VARIABLE`. Never put a secret in config JSON or the URL. Only that variable is explicitly passed into agent containers; graders receive no key. Direct copies of the key are redacted from reports, but transcripts can still contain sensitive content—review reports before sharing.

The default endpoint is `http://host.docker.internal:8080/v1` for a server on the Docker host. For your remote server, specify its LAN IP/DNS name. `localhost` **inside a container** refers to that container, not another computer. `doctor` checks Docker, `/v1/models`, and, when the base image exists, reachability from a container. It does not send an inference or validate tool calling.

## Built-in benchmarks

| Task ID | Work requested | Quantified acceptance |
|---|---|---|
| `bug-fix` | Fix invoice reconciliation precision | Decimal-only monetary arithmetic, boundary rounding, refunds and regressions |
| `small-feature` | Add nested configuration merging | Recursive merge/delete behavior, input immutability, seeded generated cases |
| `system-refactor` | Extract shared pricing across checkout/reporting | Preserved behavior, AST architecture checks, and dynamic shared delegation |
| `security-hardening` | Secure note-file reads | Invalid names, traversal, symlinks, directory-anchored reads and normal behavior |
| `performance` | Optimize sliding-window event counts | Exact randomized results, linear sequence-read work budget, generous runtime ceiling |

Public tests and a precise README contract are visible to the agent. Hidden acceptance tests are only mounted into a **different, clean grading container after the agent container has stopped**. All five starter fixtures intentionally fail some hidden checks; golden reference patches are verified by the framework's test suite, not revealed to agents.

### Metrics and interpretation

- **Correctness score (0–100):** percentage of hidden unittest methods passed. Parameterized subtests count as a single method; one failing subtest fails that method. Skips and expected failures receive no credit. Each selected task has equal weight in the harness's mean score, since repeat counts are uniform.
- **Success rate:** attempts passing every hidden check **and** completing harness execution successfully, divided by attempted cases. An agent timeout/error scores zero even if a partial patch passes; its grading remains as diagnostic evidence.
- **Completion rate:** attempts finishing as success or ordinary correctness failure, excluding timeouts/infrastructure errors. A final `length` stop is shown as a token-limit warning in the CLI and reports, with observed final output/reasoning counts. It may reflect the response budget or server context capacity; it does not replace independent correctness grading or trigger a retry.
- **Time:** host wall time including startup, grading and cleanup; separate agent and grading times are recorded. Time and tokens are measured separately, never folded into the correctness score. CPU/RAM limits constrain containers, not the remote GPU/model server.
- **Patch size:** changed production files, added/removed lines, and a unified diff. Smaller patches are not automatically better and are not rewarded in the correctness score.
- **Performance work:** element-read counts and elapsed seconds from the performance task. Work-count assertions are less noisy than strict speed thresholds.
- **Usage:** primary-stream input/output/cache tokens and tool calls when exposed by the harness. Missing/partial fields are `null`, never fabricated zeros. Summary usage totals display coverage.
- **Estimated cost:** optional USD/million-token rates. Unknown by default, even for local inference. Cache reads/writes are charged at the supplied input rate; electricity/GPU costs and hidden background calls are not measured.
- **Repeat stability:** mean scores, population score standard deviation, mean/median time. These describe the sampled attempts; this framework does **not** claim statistically reliable pass@k or confidence intervals.

Native system prompts, tool implementations and agent policies differ deliberately: the benchmark evaluates **model + harness**, not a bare model. Subagent delegation is disabled by the adapter policy; auxiliary routing is pinned to the same model. Some harnesses may still perform auxiliary calls (for example, OpenCode title generation) against that same model, and those calls may not appear in primary usage events. Native tools are enabled where available; `--tool-profile common` restricts declared tools to read/write/edit/bash, but does not make their semantics/system prompts identical.

These are small **synthetic Python tasks**, not SWE-bench or a validated production-repository leaderboard. Hidden tests improve objective grading but are not a cryptographic anti-cheating mechanism: submitted Python executes in the grader process and a deliberately malicious candidate could inspect/monkeypatch tests. AST refactor checks also encode a particular requested architecture. Scores indicate compliance with this suite, not universal software quality.

## CLI configuration

```sh
agent-bench run \
  --base-url http://192.168.1.50:8080/v1 \
  --model qwen-coder \
  --harnesses pi oh-my-pi opencode \
  --task-sets core robustness \
  --repeats 3 \
  --timeout 600 \
  --grade-timeout 60 \
  --cpus 2 \
  --memory 4g \
  --context-window 32768 \
  --max-tokens 4096 \
  --tool-profile native \
  --seed 42 \
  --output results \
  --no-build
```

| Option | Default / notes |
|---|---|
| `--model` | Required exact served model ID; also accepted from config |
| `--base-url`, `--llama-url` | `http://host.docker.internal:8080/v1` |
| `--harnesses` | All three; space-separated subset |
| `--task-sets` | One or more named sets; defaults to `default` only when neither selector is supplied |
| `--tasks` | Individual task IDs; alone runs only those tasks, or adds to explicitly selected sets |
| `--output` | `results`; always creates a unique child directory |
| `--repeats` | `1`; fresh workspaces/sessions for every attempt |
| `--timeout` | `600` seconds of agent wall time; positive finite seconds or `unlimited` |
| `--grade-timeout` | `60` seconds; always positive and finite |
| `--build-timeout` | `900` seconds **per image** |
| `--cpus`, `--memory`, `--pids-limit` | `2`, `4g`, `256`; same limits for agents and graders |
| `--context-window`, `--max-tokens` | `32768`, `4096`; positive finite integers; max tokens cannot exceed context |
| `--api-profile` | `llama-cpp`; `openai-compatible` omits llama.cpp chat-template controls |
| `--reasoning`, `--thinking` | Nonreasoning / `off`; llama-cpp profile maps off/non-off to template thinking disabled/enabled; native reasoning controls require model/endpoint support |
| `--tool-profile` | `native`; `common` for four-tool comparison |
| `--seed` | `42`; shuffles serial schedule, not sampling |
| `--api-key-env` | `AGENT_BENCH_API_KEY`; unset is fine for keyless llama.cpp |
| `--input-price`, `--output-price` | Unknown; optional nonnegative USD per million tokens |
| `--network` | `bridge`; accepts `host` or a named Docker network; graders always `none` |
| `--platform` | Native architecture; optional `linux/amd64` or `linux/arm64` |
| `--no-build` | Skip image builds; pinned local images must exist |
| `--harness-version NAME=VERSION` | Repeatable version override, recorded in reports |
| `--max-log-bytes` | `2000000` bytes per stdout/stderr stream; positive integer or `unlimited`; exceeding a finite cap aborts attempt |
| `--max-source-bytes` | `2000000` submitted source bytes, up to 500 Python files |
| `--config FILE` | JSON defaults; explicit CLI flags override them |

### Explicit unlimited agent limits

Only `run --timeout` and `run --max-log-bytes` accept the case-insensitive keyword `unlimited`. Each independently disables that framework limit and maps to `None` in `RunConfig`; omitting the option retains the finite defaults of **600 seconds** and **2,000,000 bytes per stdout/stderr stream**. NaN, infinity, zero, negative values, and booleans are invalid; use the explicit keyword rather than a numeric sentinel. Finite log caps must be integers.

For longer exploratory attempts, generally retain a finite timeout and allow complete logs:

```sh
agent-bench run --model qwen-coder --base-url http://192.168.1.50:8080 \
  --timeout 1800 --max-log-bytes unlimited --no-build

# Independently disable agent wall time while retaining the default log cap:
agent-bench run --model qwen-coder --timeout unlimited --no-build

# Fully unlimited: opt-in exploration, not a replacement for a fair bounded benchmark.
agent-bench run --model qwen-coder \
  --timeout unlimited --max-log-bytes unlimited --no-build
```

JSON config uses explicit `null` for either unlimited limit (omitted keys still use defaults). For example, save this as `exploration.json`:

```json
{
  "model": "qwen-coder",
  "base_url": "http://192.168.1.50:8080/v1",
  "timeout": null,
  "max_log_bytes": null,
  "build": false
}
```

Explicit CLI flags override JSON values. Restore one or both finite caps:

```sh
agent-bench run --config exploration.json --timeout 1800
agent-bench run --config exploration.json --timeout 600 --max-log-bytes 2000000
```

The reverse also works. With this finite config saved as `bounded.json`:

```json
{"model": "qwen-coder", "timeout": 1800, "max_log_bytes": 2000000, "build": false}
```

```sh
agent-bench run --config bounded.json --max-log-bytes unlimited
agent-bench run --config bounded.json --timeout unlimited --max-log-bytes unlimited
```

**Disabling both limits can run indefinitely, fill the host temporary disk, produce huge JSON/HTML files, and exhaust host RAM while rendering reports. Docker memory caps do not bound host logs or report rendering.** Ctrl+C uses normal interruption/cleanup and removes the active container; a hard kill is not normal cleanup. Unlimited does not guarantee completion: native tool, server, and harness errors and context-window capacity still constrain execution. Grading/build/doctor timeouts, CPU/RAM/PID limits, and submitted-source limits remain finite; `--max-tokens` remains a positive finite per-response budget.

Reports still use `schema_version: 1`. `parameters.timeout: null` and `parameters.max_log_bytes: null` mean explicitly **Unlimited**, not unknown; HTML displays `Unlimited` only for these two parameter keys and preserves JSON nulls. Other unknown metrics still display `n/a`. Finite parameters and historical measurements are unchanged. Older validators may need the latest bundled [schema](docs/REPORT_FORMAT.md) to validate unlimited runs. Correctness-only scoring is unchanged; compare time/tokens separately and disclose limits when comparing experiments.

### Selecting tasks and task sets

Each task lives in its own Python module. A task set is an explicitly named, ordered collection of task IDs, independent of task categories.

| Task set | Included tasks |
|---|---|
| `default` | `bug-fix`, `small-feature`, `system-refactor`, `security-hardening`, `performance` |
| `core` | `bug-fix`, `small-feature`, `system-refactor` |
| `robustness` | `security-hardening`, `performance` |

```sh
# One set
agent-bench run --model qwen-coder --task-sets core

# Multiple sets
agent-bench run --model qwen-coder --task-sets core robustness

# A set plus individual tasks
agent-bench run --model qwen-coder --task-sets core --tasks performance

# Individual tasks only (backward-compatible)
agent-bench run --model qwen-coder --tasks bug-fix performance
```

Sets expand in the order supplied, followed by individual tasks. Overlapping tasks are deduplicated and run **once per harness/repeat**, even with `--task-sets default core`. Explicit duplicate names, unknown IDs, and empty overall selections are rejected. The seeded shuffle is applied after expansion.

JSON configs use `"task_sets": ["core", "robustness"]` and optionally `"tasks": ["other-task-id"]` for additional tasks. Omitted/null selectors mean unspecified; if both are unspecified, `default` is selected. If either selection flag is supplied on the CLI, the CLI's selectors replace **both** config-file selectors. Other CLI overrides continue to work field by field.

Reports record the effective selection in `resolved_tasks` and `task_set_manifest`, selected set membership on each schedule entry/attempt, and summaries under `summary.per_task_set[set_id].per_harness`. A task shared by multiple selected sets contributes to each set's breakdown but only once to overall metrics; **do not sum overlapping set totals**. Old reports without task-set metadata remain readable. Individual-task-only runs have an empty set manifest and retain normal task/harness comparisons.

Run `agent-bench run --help` for the complete options. A config can use all `RunConfig` fields with underscore-separated names; unknown fields are rejected. Example:

```sh
agent-bench run --config benchmark.example.json --model my-other-served-model --repeats 5
```

Other commands:

```sh
agent-bench list --json
agent-bench build --harnesses pi --harness-version pi=0.99.2
agent-bench doctor --base-url http://192.168.1.50:8080 --model qwen-coder
agent-bench report results/RUN_ID/results.json
```

Exit status: `0` for completed benchmarks including ordinary correctness failures; `2` for configuration/infrastructure errors or attempts with errors/timeouts; `130` for interruption. Docker containers are force-removed on completion, timeout or Ctrl-C. Unfinished runs retain completed observations; a hard kill or host failure cannot guarantee cleanup. List orphan containers with `docker ps -a --filter name=agent-bench-` if necessary.

## Docker environment and reproducibility

Dockerfiles are bundled with the Python package under `src/agent_bench/docker/`:

- `Dockerfile.base`: common Node 22.19.0 / Bun 1.3.14 / Debian toolchain, Python virtualenv, Bash, Git, curl, ripgrep, jq, C/C++ build tools, pinned pytest/coverage/ruff/pyright/debugpy/IPython/Jupyter packages.
- `Dockerfile.pi`: `@earendil-works/pi-coding-agent@0.99.2`.
- `Dockerfile.oh-my-pi`: `@oh-my-pi/pi-coding-agent@18.4.8`.
- `Dockerfile.opencode`: `opencode-ai@1.18.34`.

Each child build runs its CLI's `--version` as the nonroot `bench` user (UID 1000). Package names reflect upstream's current published packages. Python/kernel/LSP/debugger dependencies allow shell/eval/code-intelligence tools; no external search credentials, desktop access or Chromium browser are provisioned.

Images use the same common base. The runner resolves **immutable local image IDs before scheduling** and records image IDs, digests where available, architecture, creation times, task fixture/test hashes, versions, parameters, and schedule. It then executes those IDs, not mutable tags. This keeps a run consistent even if local tags change.

Harness versions and primary tool versions are pinned; Docker tag manifests, Debian packages, Python transitive dependencies, and some harness transitive dependencies can still change between builds. For exact cross-machine replay, retain/export the images from the report (e.g. `docker save`), use `--no-build`, and hold server weights, quantization, sampling, server build and hardware constant. Timings are not comparable across different host hardware or architectures; emulated `--platform` runs are particularly different.

Agent containers have a fresh writable home, an ephemeral `/workspace`, and individual read-only harness config files. The caller's actual home, report directory, hidden tests, Docker socket, and model weights are never mounted. Containers run nonroot, with dropped capabilities, no-new-privileges and finite resource limits; agent wall-time/output limits apply unless explicitly disabled as described above.

On Linux with a nonroot caller, agents run with the caller's numeric UID/GID so newly created files, Python caches, and private directories remain readable/removable by the host. A separate private temporary home is mounted at `/home/bench`; config-file mount points are prepared inside it, and that home is removed after container shutdown. No actual host-home data is exposed. On macOS and root-host runs, agents retain the image's nonroot `bench` user (UID 1000); root callers are never mapped to container root. Graders always retain the image's nonroot user. Initial workspace modes remain permissive inside a private temporary directory, but permissions alone are not used to compensate for mismatched Linux ownership.

Only regular UTF-8 `solution/**/*.py` files enter grading; candidate tests, config, non-Python artifacts and symlinks are excluded/rejected. Grading uses the clean common base, a read-only root filesystem, read-only source/test mounts, a bounded `/tmp`, no API credentials, and **network disabled**. Candidate code never executes on the host.

Docker is a repeatability/isolation mechanism, not a guarantee against hostile code or kernel vulnerabilities. Agent containers need network access to the model server and can reach other services allowed by that network. The framework does not implement an outbound endpoint-only firewall or limit model-server resource consumption. Run untrusted experiments on a dedicated machine/VM with appropriate LAN firewall policies.

## Development and extending the suite

Install the optional development tools (the CLI still has no runtime dependencies):

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pyright src tests scripts

# Opt-in real Docker/CLI tool-calling tests, using a local mock API, not paid inference:
.venv/bin/agent-bench build
AGENT_BENCH_DOCKER_TESTS=1 .venv/bin/python -m unittest discover \
  -s tests -p test_docker_integration.py -v
```

GitHub Actions runs unit checks on Linux/macOS, quality checks, and clean distribution-install checks. Docker integration runs weekly and when manually selected, without a LAN model or inference credentials. Build inputs require upstream network access. See [CONTRIBUTING.md](CONTRIBUTING.md) for development details and [the release checklist](docs/RELEASING.md) before publishing or tagging a release. PyPI publishing is not configured.

Ordinary tests cover CLI defaults/overrides, adapters and usage parsing, safe source snapshots, lifecycle/timeout handling, report escaping and aggregation, and baseline/golden task grading. The opt-in integration test drives actual tools through all three Docker harnesses and runs every baseline in a clean grading container. Mock inference validates wiring, **not real llama.cpp/model quality**.

### Adding tasks and task sets

Task definitions are in `src/agent_bench/tasks/`, one module per task. Each exports `TASK`, constructed with `Task` or `create_task` from `tasks/base.py`. The factory takes visible production modules, public tests, an explicit prompt/contract, hidden stdlib unittest source, and optional metadata. Its optional `category=` keyword lets different tasks share a category; categories do not determine set membership.

To add a task:

1. Create a module such as `tasks/parser_bug.py` exporting `TASK`. Give it a unique stable ID; production Python belongs under `solution/`.
2. Import it in `tasks/__init__.py` and add it to the explicit registry tuple. Duplicate IDs fail immediately.
3. Add baseline and golden reference grading coverage in `tests/test_tasks.py` and update catalog expectations/preservation hashes when intentionally changing fixtures.
4. Add its ID to the appropriate task sets. Registering a task alone does **not** change the default set or silently broaden existing benchmarks.

Task sets are in `src/agent_bench/task_sets/`, one module per set. For example:

```python
from .base import TaskSet

TASK_SET = TaskSet(
    id="parser-work",
    title="Parser maintenance",
    description="Bug fixing and feature work in parsers.",
    task_ids=("parser-bug", "parser-feature"),
)
```

Import the new `TASK_SET` in `task_sets/__init__.py` and include it in the registry tuple. Sets must be nonempty and reference existing task IDs without duplicate members. The explicit registries automatically populate CLI choices, listings, selection validation, and report manifests. No filesystem scanning or category inference is used. Definitions are trusted framework code, not user-supplied plugins.

To add a harness, implement its provider/config/argv and event adapter in `harnesses.py`, add a child Dockerfile and adapter/integration tests.

This project is independent of the Pi, oh-my-pi, OpenCode, and llama.cpp teams. MIT licensed.

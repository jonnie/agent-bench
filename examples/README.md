# Measured example reports

These are **real measured results**, not mocked output or a benchmark leaderboard.
The private completed source run is `20261002T123149Z-eeb7b0c5`, recorded on
2026-10-02 from 12:31:49 to 12:56:38 UTC. Its reported server alias was
`qwen3.8-27b`; the alias was **not independently verified** as a particular model,
weights revision, or parameter count.

- [Standalone offline HTML report](qwen3.8-27b/results.html): clone/download the
  repository and open this file in a browser. GitHub's file view is not an HTML
  report host. No HTTP server, JavaScript, CDN, or external assets are required.
- [Sanitized measured JSON](qwen3.8-27b/results.json): all 45 attempts, including
  unsuccessful attempts, independent grading details, production patches,
  timing, usage, and termination warnings.
- [Repository / clone source](https://github.com/jonnie/agent-bench)
- [Report file in the repository](https://github.com/jonnie/agent-bench/blob/main/examples/qwen3.8-27b/results.html)
- [Framework methodology and setup](../README.md)
- [Bundled schema-v1 contract](../src/agent_bench/schemas/results-v1.schema.json)

## Observed results

| Harness | Recorded version | Fully successful attempts | Success rate | Mean correctness / 100 |
|---|---|---:|---:|---:|
| oh-my-pi | 18.4.8 | 13 / 15 | 86.67% | 97.50 |
| pi | 0.99.2 | 9 / 15 | 60.00% | 93.17 |
| OpenCode | 1.18.34 | 10 / 15 | 66.67% | 91.50 |
| **Overall** | | **32 / 45** | **71.11%** | **94.06** |

All attempts completed harness execution: 32 were successful and 13 were ordinary
correctness failures; none were execution errors or timeouts. The report retains
two final-response `length` warnings (pi, `small-feature` repeat 2 and
`security-hardening` repeat 1). These are observations about response termination,
not inferred causes of failure or changes to independent grading.

There are **five small synthetic Python tasks**, three repeats per task per
harness: `bug-fix`, `small-feature`, `system-refactor`, `security-hardening`, and
`performance`. That is 5 × 3 × 3 = 45 attempts, **not 45 independent problems**.
Each attempt used a fresh workspace/container and the attempts ran serially in a
seeded shuffled order. Native prompts, tools and policies differ across harnesses;
this measures **model + harness**, not a bare model. Subagent delegation was
disabled by the adapter policy.

Correctness is the percentage of hidden unittest methods passed. Full success
requires every hidden check to pass and successful harness execution. All attempts
count in the denominator. Tasks have equal weight because repeat counts are
uniform. Runtime, patch size, token usage and cost do not weight correctness.
Repeats are descriptive observations, not pass@k estimates, confidence intervals,
or evidence of broad production coding ability. Synthetic tests are not a
contamination-resistant or cryptographically secure evaluation.

Timing is host wall time (including startup, grading and cleanup), with separate
agent/grader times retained. Usage comes from primary harness event streams;
background calls may be missing. Missing values remain unknown, and reported
cache/token counts should not be assumed to have identical accounting across
harnesses. No token prices were supplied; costs are unknown.

## Provenance, versions and limitations

The source records framework version `0.1.0` and `schema_version: 1`. It **predates
task-set selection metadata**: only the measured explicit task list and schedule
are published. No `task_set_manifest`, `resolved_tasks`, attempt memberships or
measured set selection were retroactively invented. The current
`agent_bench.report.write_reports` renders this legacy-compatible artifact and
recalculates its summary from unchanged attempts; the recomputed summary is
identical to the source summary. Renderer and source version information are in
`publication`. A version number alone does not identify the exact source commit.

Recorded Python, Docker/component versions, architectures, image IDs and creation
times, harness versions, task fixture/hidden-test hashes and submitted-source
hashes are retained. The source host is Darwin/arm64 with Python 3.14.0, while the
recorded base and harness images are Linux/amd64. No explicit platform override
was recorded. These observations do not establish server hardware or make timing
portable to a different deployment.

**This is not a completely reproducible deployment.** The original report does
not record verified model weights/revision, quantization, model-server hardware,
llama.cpp build/launch flags, sampling/temperature/server seed, or prompt-cache
settings. Those are unknown, not guessed. The recorded `llama-cpp` API profile
and context/request settings are client configuration, not proof of the server
implementation or effective server limits. Local image IDs were captured, but
images were not exported with this example; their IDs alone do not make them
available to another machine. Docker builds and transitive dependencies may have
changed. Results and timings are not promised to replay exactly.

## Publication sanitization

The checked-in JSON's `publication` object explicitly lists removals and every
replaced endpoint path. The HTML is generated **only from sanitized data** and
embeds that same JSON, not the private original.

- Every attempt has `logs: {}`. Full stdout/stderr, grader stdout/stderr and agent /
  tool transcripts are omitted, not selectively excerpted. Parsed grading cases,
  traceback details, metrics, usage and termination observations are retained.
- The endpoint is replaced everywhere it was recorded, including all 45 nested
  `harness_configuration.base_url` fields, with
  `http://model-server.example.invalid:8080/v1`. This is a deliberately
  non-resolving publication placeholder, **not the measured endpoint**.
- The local output-directory parameter and Docker daemon CPU/RAM inventory
  (`NCPU`, `MemTotal`) are removed. No usernames, home paths or private directory /
  host labels remain in the published report. Generic container paths in grading
  tracebacks and production diff paths remain diagnostic evidence.
- `localhost` and `'password': 'secret'` in two grading assertions are literal
  synthetic inputs from `src/agent_bench/tasks/small_feature.py`, **not** a host
  endpoint or real password. `AGENT_BENCH_API_KEY` is the standard variable name,
  not a credential value; the source records `credential_present: false`.
- The source run ID and SHA-256 of the original private JSON identify provenance.
  No raw logs, original LAN address or secret values are included in metadata.

The original LAN reports under `results/` remain private and unchanged. Do not
publish them in place of these examples. The scrub script is deliberately limited
to this reviewed source checksum: it is **not a generalized privacy guarantee**.
Review both JSON and HTML (including patches, grading errors and embedded source
data) before sharing any report.

## Regenerate the example without inference

From a repository checkout with Python 3.11+:

```sh
PYTHONPATH=src python3 scripts/make_example.py \
  --source results/20261002T123149Z-eeb7b0c5/results.json \
  --output examples/qwen3.8-27b
```

The private source is intentionally not included in a public clone. Its SHA-256
is `8883129e4ed4eea3d2e371efcc3faaebb1500737d38d47b3a7e5dc1f26649035`.
The script accepts an explicit source location and a separate output directory,
checks that exact source, preserves its measurements, refuses to overwrite the
source report directory, and calls the current report writer. It uses only the
stdlib and framework imports; it performs no Docker operations, network calls,
or inference. Manual review remains necessary.

In a public clone, re-render the **already sanitized** JSON with the installed
CLI (also offline, with no inference):

```sh
git clone https://github.com/jonnie/agent-bench.git
cd agent-bench
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/agent-bench report examples/qwen3.8-27b/results.json
```

Installation may fetch build tooling; report rendering itself has no runtime
package dependencies or network requirement. A future renderer may change HTML
presentation without changing the measured observations.

## Run a comparable experiment

The following command expresses the **recorded client arguments**, using an
explicit task list rather than inventing historical task-set selection. The
original invocation text was not captured; this command is reconstructed from
`parameters`, not asserted to be the original shell command. Replace **only**
the publication endpoint with your reachable server endpoint, and use the alias
only if it is actually advertised by that server. Setup/build/server preparation
is described in the [main README](../README.md).

```sh
.venv/bin/agent-bench run \
  --base-url http://model-server.example.invalid:8080/v1 \
  --model qwen3.8-27b \
  --harnesses pi oh-my-pi opencode \
  --tasks bug-fix small-feature system-refactor security-hardening performance \
  --repeats 3 \
  --timeout 600 \
  --grade-timeout 60 \
  --build-timeout 900 \
  --cpus 2 \
  --memory 4g \
  --pids-limit 256 \
  --context-window 32768 \
  --max-tokens 4096 \
  --no-reasoning \
  --thinking off \
  --api-profile llama-cpp \
  --tool-profile native \
  --seed 42 \
  --api-key-env AGENT_BENCH_API_KEY \
  --network bridge \
  --harness-version pi=0.99.2 \
  --harness-version oh-my-pi=18.4.8 \
  --harness-version opencode=1.18.34 \
  --max-log-bytes 2000000 \
  --max-source-bytes 2000000 \
  --output results
```

Image building is enabled, matching the source (`build: true`); no platform or
price override was recorded. The example run had no API credential present.
Explicit `--tasks` in the current CLI does not select any named task set. This
command describes a comparable new measurement, not exact deployment replay;
it will perform inference against your configured server. No new inference was
performed to prepare these published examples.

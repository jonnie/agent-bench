# Report format: `schema_version: 1`

[agent-bench](https://github.com/jonnie/agent-bench) writes `results.json` and a
standalone, offline `results.html` in one unique directory per run. JSON is the
machine-readable record; HTML presents the same record, including its JSON.
The bundled structural contract is
[`results-v1.schema.json`](../src/agent_bench/schemas/results-v1.schema.json), using
**JSON Schema draft 2020-12**. This document describes the current producers in
`runner.py`, `report.py`, and `harnesses.py`; it does not claim a live model run
has been performed or verified.

## Validation and compatibility

The root requires `schema_version`, `framework_version`, `run_id`, `created_at`,
`finished_at`, `status`, `model`, `parameters`, `environment`, `task_manifest`,
`results`, `summary`, and `methodology`. `schema_version` must be the integer `1`,
not a string or boolean. `framework_version` identifies the producer release;
it is not the report format version.

Unknown properties are generally allowed so consumers can retain future
additions. Known properties still have types and bounds: counts are nonnegative
integers, durations and costs are nonnegative numbers, scores are in `[0, 100]`,
rates are in `[0, 1]`, and booleans are not numbers. Repeat indexes and configured
positive limits start at `1` or must be greater than zero, as appropriate. Exit
codes are signed integers, not nonnegative counts. Provider termination reasons
remain open strings; run, attempt, and grading-case statuses have explicit enums.
Task/harness IDs and task-specific metadata are not frozen to the current catalog.

Task-set additions are **optional** throughout version 1: `parameters.task_sets`,
`task_set_manifest`, `resolved_tasks`, schedule/attempt `task_sets`,
`summary.per_task_set`, and `methodology.task_sets`. The later `termination`
diagnostic and summary coverage/spread fields are also not required. Historical
producer-shaped schema-1 reports without these additions remain valid; absence
must not be filled in by guessing membership or observations from today's registry.

Explicit unlimited agent limits are another version-1 extension:
`parameters.timeout` accepts a positive number or `null`, and
`parameters.max_log_bytes` accepts a positive integer or `null`. Here `null`
means explicitly **Unlimited**, not an unknown measurement; no other configured
limit gains nullable/unlimited semantics. New reports still use `schema_version: 1`.
Older validators may need the **latest bundled schema** to validate unlimited
runs. Finite parameters and historical measured data remain unchanged; do not
rewrite old evidence to add unlimited settings.

The renderer accepts some more loosely shaped dictionaries for presentation;
that does not make every such dictionary a valid producer report.

Consumers can validate locally with the optional development/consumer dependency
`jsonschema==4.23.0`; agent-bench does not require a runtime validation dependency
and the runner does not automatically validate against this schema:

```python
import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

schema = json.loads(
    files("agent_bench.schemas")
    .joinpath("results-v1.schema.json")
    .read_text(encoding="utf-8")
)
Draft202012Validator.check_schema(schema)
validator = Draft202012Validator(schema, format_checker=FormatChecker())
report = json.loads(Path("results/RUN_ID/results.json").read_text(encoding="utf-8"))
validator.validate(report)
```

The resource must be included as package data in built distributions. Validation
uses local schema references; it does not need to contact GitHub or a model API.
`format` checking is opt-in in JSON Schema implementations; enable it to check
`date-time` strings. In `jsonschema`, date-time checking also needs its optional
format support (for example `jsonschema[format]==4.23.0`); without that support,
`FormatChecker` does not check date-time calendar semantics. The schema separately
constrains timestamp shape without that extra dependency. The writer uses strict
JSON serialization (`allow_nan=False`),
so NaN and infinity are not report values.

This is a structural contract, not a proof of correctness, provenance, arithmetic
consistency, or benchmark integrity. It does not check that summary totals match
attempts, grader counts sum to the total, score equals passed/total, timestamps
are ordered, or schedule/task references match. The runner separately checks the
core grader counts, score, success flag, and number of cases. Preserve unknown
fields when processing reports; do not silently change version numbers or rewrite
old evidence to resemble a newer run. Incompatible changes need a separately
versioned contract rather than tightening this one to exclude old reports.

## Run lifecycle and metadata

| Field | Meaning |
| --- | --- |
| `run_id` | UTC timestamp plus a short random suffix; also the output directory name. |
| `created_at`, `finished_at` | UTC date-time strings; `finished_at` is `null` while running. |
| `status` | `running`, `completed`, `interrupted`, or `error`. |
| `error` | Optional explanation of a run-level interruption/infrastructure failure. |
| `model` | Selected model ID, normalized API base URL, context/output token limits, reasoning/thinking settings, and API profile. These are configuration metadata, not verification of server behavior. |
| `parameters` | Effective `RunConfig`, including selectors, harness versions, scheduling seed, resource/time/output limits, optional prices, and credential variable name. |
| `environment` | Host OS, architecture and Python version, credential-presence boolean, and, when obtained, Docker/daemon and image metadata. |
| `task_manifest` | Ordered selected tasks: ID, title, category, visible prompt, metadata, and SHA-256 hashes of fixtures and hidden-test source. |
| `methodology` | Human-readable explanations of scoring, execution, usage, limits, and scope. |
| `schedule` | Optional seeded-shuffled execution order, with harness, task ID, one-based repeat index, and optional selected-set memberships. |
| `results` | Observations for started attempts, in actual execution order; not placeholders for all scheduled attempts. |
| `summary` | Derived overall, per-harness, and optional per-task-set metrics. |

The runner persists a `running` report **before Docker availability is checked**,
then after scheduling and each attempt, and finally on completion or handled
failure/interruption. A startup-error report can have no results, no schedule,
and no Docker/image fields. Those fields must not be required just because they
are present in a successful run. An interrupted attempt can retain partial logs,
usage, grading, and patch data. A hard kill can leave the last `running` snapshot;
it is not evidence that every scheduled attempt finished.

`completed` means the schedule was exhausted, **not** that all attempts succeeded.
Ordinary correctness failures, timeouts, and nonfatal attempt errors remain in
that report. Fatal container-cleanup failures set an attempt's `abort_run` and
stop the run with status `error`.

Docker metadata includes the reported version and daemon OS/architecture,
`NCPU` (CPU count), and `MemTotal` (bytes), which may be unknown (`null`). Image
records contain tag, immutable local ID, repository digests where available,
architecture, OS, and creation time. The runner executes recorded immutable IDs,
not tags. This metadata is optional when startup fails and does not identify
remote model weights, sampling, quantization, hardware, or server build.

## Task sets: ordered union, not duplicated work

When both task selectors are unspecified, the `default` set is selected. Otherwise
selected sets expand in supplied order, followed by explicit task IDs. A task's
first occurrence determines its position in `resolved_tasks`; overlaps are
removed before scheduling. Explicit duplicate selector names and empty overall
selections are rejected. Each resolved task runs once per harness/repeat.

`task_set_manifest` records each selected set's ID, title, description, and ordered
`task_ids`. Individual-task-only runs have an empty manifest. Membership in a
set is explicit, not inferred from task category. `task_sets` on schedule entries
and results lists memberships among the **selected** sets, not every set in the
live registry.

`summary.per_task_set[set_id]` has the same metrics as the overall summary,
including `per_harness`. The summarizer groups by the **recorded manifest's task
IDs**, not by today's catalog or the result's membership label. With selected
sets `default` and `core`, a shared task appears in both set summaries, but only
once in overall/per-harness metrics. **Never sum overlapping set totals** to get
overall attempts, tokens, cost, or scores. Direct tasks outside selected sets
contribute overall but not to a set breakdown.

## Attempts and correctness

Every attempt contains harness/task identity, title/category, one-based `repeat`,
`status`, `score`, `duration_seconds`, nullable `agent_exit_code`, nullable
`grading`, `usage`, `patch`, `logs`, and start/finish timestamps. The current
producer also initializes `termination`; it is optional for older version-1
reports. Empty `{}` diagnostics are valid when execution never got that far.
Do not require prompt, adapter configuration, source hash, agent/grader durations,
or grader exit code for a setup failure.

Attempt statuses are:

- `success`: grading passes every hidden case and agent execution succeeds.
- `failed`: grading completes but not all hidden cases pass.
- `timeout`: agent or grading time limit exceeded.
- `error`: harness, grading-process, configuration, output-limit, snapshot, or
  infrastructure failure.
- `interrupted`: the started attempt was interrupted by the user.

New attempts include optional `outcome_reason`, a human-readable explanation of the
result. Correctness failures name failing hidden checks and summarize the final
assertion/exception; execution failures include the execution error and observed
tool activity/stop reason when available. A retained grade after execution failure
is explicitly diagnostic and cannot earn successful-attempt credit. This string is
not a stable machine-readable category: use `status`, `grading.cases`, and numeric
metrics for analysis.

`error` remains an execution-error field. Its absence on a `failed` attempt means
the harness completed normally but the submitted code did not pass all checks;
it does **not** mean the failure cause is unknown. HTML separates **Outcome** from
**Execution error**, and derives an explanation from existing grading data when
rendering historical reports without `outcome_reason`. Rendering does not change
recorded JSON or scores. This is an additive schema-version-1 field, not a schema
version bump.

`grading`, when available, records `tests_total`, `tests_passed`, `tests_failed`,
`tests_errors`, `score`, `success`, and `cases`. Each case has `name`, `status`
(`passed`, `failed`, or `error`), and `detail`. Skips and expected failures receive
no credit and are represented as failed cases, not a fourth case status.
The grader may include its own `duration_seconds` and task-specific `metrics`.
For example, performance metrics can record element-read work and elapsed seconds;
these have task-specific meanings, not a universal efficiency score.

Correctness is `round(100 × tests_passed / tests_total, 2)`. The unit is percentage
points, **not** a fraction. Each unittest method is one scored case; subtests do
not become separately weighted cases. An agent timeout, execution error, or
interruption receives attempt score `0`, even if retained grading shows a passing
partial patch. Keep attempt status/score distinct from that diagnostic grade.
A final token-limit warning alone does not reduce score or change status.

`patch` includes `files_changed`, `lines_added`, `lines_removed`, and unified
`diff` for submitted production Python. `source_sha256` hashes the serialized
source snapshot when available. Only regular UTF-8 `solution/**/*.py` files are
submitted to the independent grader; candidate tests/configs are not submitted.
Patch counts are descriptive, not correctness rewards or proof of source safety.
`logs` may include agent `stdout`/`stderr` and grader `grader_stdout`/`grader_stderr`.
`prompt` includes the visible task and runner instructions. `harness_configuration`
is reportable adapter configuration; extra effective request controls can be added.

## Units, usage, and termination

- All `*_duration_seconds` fields and `grading.duration_seconds` are wall-clock
  **seconds**. Attempt duration includes host preparation, agent execution,
  independent grading, and cleanup. Agent/grader execution durations are separate
  diagnostics, not remote inference-only timings or components that must sum to
  the attempt duration.
- `timeout`, `grade_timeout`, and `build_timeout` are seconds; `cpus` is a Docker
  CPU limit, `memory` is a Docker size string, `pids_limit` counts processes, and
  log/source limits are bytes. The default agent `timeout` is 600 seconds and
  `max_log_bytes` is 2,000,000 bytes per stdout/stderr stream. A positive finite
  value preserves the cap; `null` for either of these two parameters independently
  disables it. HTML parameter lists display **Unlimited** for those nulls only,
  while embedded/full JSON retains `null` without mutating the record. Missing
  parameters do not imply unlimited; unknown metrics still display `n/a`.
  Grading/build/doctor timeouts and CPU/RAM/PID/source limits remain finite.
  Docker limits constrain client containers, not remote GPU/RAM or total model
  tokens, host log storage, or host report-rendering memory.
- `context_window` and `max_tokens` are token counts. `max_tokens` is a
  per-response setting, not a total attempt budget. The scheduling `seed`
  shuffles attempt order; it does not make server sampling deterministic.
- Usage fields are primary-stream `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, and `tool_calls`. Known counts are
  nonnegative integers. Missing/partial observations are `null`, **not zero**;
  early failures may instead leave an empty usage object. A known zero is retained.
  If any observed response omits a token field, that attempt's field is unknown.
- The parser aggregates final assistant/step events, not streaming snapshots.
  Stable-ID/timestamp response replays and identified tool calls are deduplicated;
  distinct ID-less response events are not collapsed solely by matching content.
  Primary usage can omit background/auxiliary requests, so it is not a complete
  server billing or token audit.
- Input/output/cache accounting is harness/provider-specific. The report records
  emitted counts; it does not establish a universal cache-inclusive input total.
  **Do not blindly add cache counts to input for cross-provider comparisons**:
  cache accounting can overlap or differ. Preserve the individual counters and
  explain the provider's accounting when publishing derived totals.
- `input_price` and `output_price` are **USD per million tokens**;
  `usage.estimated_cost_usd` is **USD**. The current estimate requires all four
  token counts and both prices, and computes
  `(input + cache_read + cache_write) / 1_000_000 × input_price +
  output / 1_000_000 × output_price`. Reads/writes use the input rate, not unknown
  provider-specific cache tariffs. `cost_note` states these limitations. This
  formula is an estimate, not a guarantee against provider overlap or double
  charging, and excludes hardware/electricity costs and unobserved calls.
  Unknown cost stays `null`, including for local inference without supplied prices.
- `termination` describes only the **last emitted completed assistant response**:
  `reason`, `output_tokens`, `reasoning_tokens`, and `warning` can be unknown.
  Later responses replace earlier diagnostics, even if the later fields are
  unknown. Reasoning tokens are never inferred from text or subtracted from output;
  reasoning/output accounting can overlap. A `length` reason adds a warning about
  potentially incomplete output, without identifying which server/response limit
  caused it, triggering a retry, or changing correctness/status.

### Unlimited configuration and operational limits

The case-insensitive CLI keyword `unlimited` maps to `RunConfig` `None` and then
JSON `null` only for `run --timeout` and `run --max-log-bytes`. JSON config can
supply those nulls directly. Omitted options retain finite defaults; explicit CLI
flags override JSON settings in either direction. NaN, infinity, zero, negatives,
and booleans are invalid limits; `max_log_bytes` and `max_tokens` require positive
finite integers. `max_tokens` remains bounded by the configured context window.

A practical exploratory run retains finite wall time and unlimited logs:

```sh
agent-bench run --model qwen-coder --timeout 1800 --max-log-bytes unlimited --no-build
```

For fully unlimited opt-in exploration, a config can contain:

```json
{"model": "qwen-coder", "timeout": null, "max_log_bytes": null, "build": false}
```

Disabling both limits can run indefinitely, fill host temporary disk, produce
huge JSON/HTML reports, and exhaust host RAM during rendering. Docker memory
caps do not bound host logs. Ctrl+C invokes normal interruption cleanup and
removes the active container; hard kills cannot guarantee cleanup. Native tool,
server, and harness errors and context-window capacity still constrain execution:
unlimited does not guarantee completion. Fully unlimited exploration is not a
replacement for a fair bounded benchmark. Correctness-only scoring is unchanged;
time and tokens remain separate measurements, not efficiency weights.

## Summaries and null denominators

Overall summary and each harness/set group use the same definitions:

| Metric | Definition |
| --- | --- |
| `attempts`, `successes` | Recorded observations, and those with status `success`. |
| `success_rate` | Successes / recorded attempts; fraction `[0, 1]`, or `null` for zero attempts. |
| `completion_rate` | Attempts with status `success` or `failed` / recorded attempts; `null` for zero attempts. |
| `mean_score` | Mean of known attempt scores, not pooled hidden-test counts; `null` without known scores. |
| `score_stddev` | Population standard deviation of known scores; `null` without known scores. |
| `mean_duration_seconds`, `median_duration_seconds` | Known attempt durations only; `null` without observations. |
| `scores_known`, `durations_known` | Counts of observations included in score/time calculations. |
| `tokens`, `tool_calls`, `estimated_cost_usd` | Totals of known per-attempt fields only. Unknown-only totals are omitted, not replaced by zero. |
| `usage_samples` | Per-field number of attempts contributing to usage/cost totals, including known zeros. |

Rates are not percentages; HTML can display them as percentages. Usage coverage
can differ by field: a cost total and a token total need not cover the same
attempts. Read `usage_samples` before comparing totals. Empty reports have no
known token totals and null rates/means; absence of `usage_samples` in older
reports means coverage was not recorded. The writer recomputes summary from
results and the recorded set manifest when producing both files.

Uniform completed schedules weight each task equally because repeat counts are
uniform. Partial runs reflect only started/recorded attempts and can have unequal
task/harness coverage; do not treat their means as complete-run comparisons.
Repeats describe this run, not pass@k, confidence intervals, or a statistically
validated ranking. No score weighting by cost, time, or patch size is applied.

## Privacy and publication

Reports contain prompts, generated text, tool transcripts, diffs, hidden-case
names/details, configuration, endpoint addresses, host/Docker metadata, and local
output paths. HTML also contains the JSON, so it needs the **same review** as JSON.
The runner redacts direct occurrences of the configured API-key value in string
values; `api_key_env` names a variable, and `credential_present` is only a boolean.
This is not general-purpose sanitization: other credentials, transformed secrets,
identifying paths/hosts, private data, or sensitive generated content can remain.
Schema validity does not certify that a report is safe to publish.

Before sharing either file:

1. Review all logs, prompts, diffs, grader details, URLs, environment, and extra
   properties. Remove secrets and private identifiers from **both** documents.
2. Keep an unmodified private original; label any public copy as redacted and
   describe transformations. Retain units, unknowns, schema/version metadata,
   status, coverage, and the recorded manifests. Do not fabricate missing evidence.
3. Publish selected task IDs/sets, repeats, tool/API/thinking profiles, harness and
   framework versions, resource limits, image IDs/digests, and fixture/test hashes.
   Record server weights/quantization, template, sampling, server version and
   hardware separately when known; the report does not collect or verify them.
4. State whether the run is complete or partial and whether observations came
   from a real model, a mock, or another synthetic setup. Schema tests use mocked
   Docker execution and grades; they establish format compatibility, **not** live
   inference, patch quality, tool-calling reliability, or benchmark performance.

The tasks are small synthetic Python exercises, not a production-repository or
contamination-resistant benchmark. Native harness policies, tools, and system
prompts differ, even in the common tool profile. Hidden grading improves objective
checks but is not cryptographic anti-cheating: submitted Python executes in the
grader process. Docker isolation is not a guarantee against hostile code. Report
scores as results for the documented **model + harness + task suite + environment**,
not as universal model quality or a validated leaderboard.

Each file is replaced atomically after staging, but the JSON/HTML pair is not one
transaction. A crash between replacements can leave different generations.
Compare run/status/result metadata when archiving; retain the JSON as primary
machine-readable evidence and disclose any regenerated/redacted HTML.

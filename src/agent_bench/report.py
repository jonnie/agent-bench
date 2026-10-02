"""Aggregate benchmark results and write portable, offline reports.

Scores and durations are averaged over known values, not over all attempts.
Score spread is the population standard deviation: repeats describe this run,
not an estimate of pass@k. Usage totals cover only attempts reporting that field;
``usage_samples`` records their coverage. No efficiency weighting is applied.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Any

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_USAGE_FIELDS = (*_TOKEN_FIELDS, "tool_calls", "estimated_cost_usd")


def _number(value: Any) -> int | float | None:
    """Reject nonnumeric/nonfinite values without mistaking bools for counts."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _metrics(results: list[dict]) -> dict:
    scores = []
    durations = []
    usage_values: dict[str, list[int | float]] = defaultdict(list)
    for result in results:
        score = _number(result.get("score"))
        if score is not None:
            if not 0 <= score <= 100:
                raise ValueError("Scores must be between 0 and 100")
            scores.append(score)
        duration = _number(result.get("duration_seconds"))
        if duration is not None:
            if duration < 0:
                raise ValueError("Durations must be nonnegative")
            durations.append(duration)
        usage = result.get("usage") or {}
        for field in _USAGE_FIELDS:
            value = _number(usage.get(field))
            if value is not None:
                if value < 0:
                    raise ValueError(f"Usage field {field} must be nonnegative")
                usage_values[field].append(value)

    attempts = len(results)
    successes = sum(result.get("status") == "success" for result in results)
    completed = sum(result.get("status") in {"success", "failed"} for result in results)
    metrics = {
        "attempts": attempts,
        "successes": successes,
        "success_rate": successes / attempts if attempts else None,
        "completion_rate": completed / attempts if attempts else None,
        "mean_score": statistics.mean(scores) if scores else None,
        "mean_duration_seconds": statistics.mean(durations) if durations else None,
        "median_duration_seconds": statistics.median(durations) if durations else None,
        "score_stddev": statistics.pstdev(scores) if scores else None,
        "scores_known": len(scores),
        "durations_known": len(durations),
        "tokens": {
            field: sum(usage_values[field]) for field in _TOKEN_FIELDS if usage_values.get(field)
        },
        "usage_samples": {field: len(values) for field, values in usage_values.items()},
    }
    for field in ("tool_calls", "estimated_cost_usd"):
        if usage_values.get(field):
            metrics[field] = sum(usage_values[field])
    return metrics


def summarize(results: list[dict], task_sets: list[dict] | None = None) -> dict:
    """Return overall metrics plus a ``per_harness`` mapping.

    Every result counts as an attempt, including failed, timeout and error
    results. Success is determined by status, independently of score. Completion
    means status success or failed (a timeout/error is not completed). Undefined
    rates/means are None. Known zero usage is included; missing usage is omitted
    from totals rather than silently counted as zero. Inputs are never mutated.

    When supplied, the recorded task-set manifest adds ``per_task_set`` summaries
    with the same metrics and per-harness breakdowns, grouped by its task IDs.
    Overlapping sets share observations without duplicating overall totals.
    None preserves the legacy output; an empty manifest adds an empty mapping.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        harness = result.get("harness")
        groups[str(harness) if harness is not None else "Unknown"].append(result)
    summary = _metrics(results)
    summary["per_harness"] = {harness: _metrics(group) for harness, group in sorted(groups.items())}
    if task_sets is not None:
        summary["per_task_set"] = {}
        for task_set in task_sets:
            task_ids = set(task_set.get("task_ids") or [])
            group = [result for result in results if result.get("task_id") in task_ids]
            summary["per_task_set"][task_set["id"]] = summarize(group)
    return summary


def _text(value: Any) -> str:
    """Escape all dynamic content, including JSON and attribute values."""
    if value is None:
        return "n/a"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return escape(str(value), quote=True)


def _json(value: Any) -> str:
    return escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), quote=True)


def _format(value: Any, *, percent: bool = False, seconds: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    if percent:
        return f"{number:.1%}"
    if seconds:
        return f"{number:,.2f} s"
    return f"{number:,.2f}"


def _pairs(values: dict) -> str:
    if not values:
        return '<p class="muted">n/a</p>'
    return (
        '<dl class="pairs">'
        + "".join(f"<dt>{_text(key)}</dt><dd>{_text(value)}</dd>" for key, value in values.items())
        + "</dl>"
    )


def _pre(value: Any) -> str:
    return f"<pre>{_text(value)}</pre>"


def _usage(metrics: dict) -> str:
    totals = dict(metrics.get("tokens") or {})
    for field in ("tool_calls", "estimated_cost_usd"):
        if field in metrics:
            totals[field] = metrics[field]
    if not totals:
        return '<span class="muted">n/a</span>'
    samples = metrics.get("usage_samples") or {}
    attempts = metrics.get("attempts", 0)
    return (
        '<ul class="usage">'
        + "".join(
            f"<li>{_text(field)}: <strong>{_text(value)}</strong> "
            f'<span class="muted">({_text(samples.get(field))}/{_text(attempts)} attempts)</span></li>'
            for field, value in totals.items()
        )
        + "</ul>"
    )


def _comparison(per_harness: dict) -> str:
    rows = []
    for harness, metrics in per_harness.items():
        rows.append(
            f'<tr><th scope="row">{_text(harness)}</th>'
            f"<td>{_text(metrics.get('attempts'))}</td>"
            f"<td>{_text(metrics.get('successes'))}</td>"
            f"<td>{_format(metrics.get('success_rate'), percent=True)}</td>"
            f"<td>{_format(metrics.get('completion_rate'), percent=True)}</td>"
            f"<td>{_format(metrics.get('mean_score'))}</td>"
            f"<td>{_format(metrics.get('score_stddev'))}</td>"
            f"<td>{_format(metrics.get('mean_duration_seconds'), seconds=True)}</td>"
            f"<td>{_format(metrics.get('median_duration_seconds'), seconds=True)}</td>"
            f"<td>{_text(metrics.get('scores_known'))} / {_text(metrics.get('durations_known'))}</td>"
            f"<td>{_usage(metrics)}</td></tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="11" class="muted">No attempts recorded.</td></tr>')
    return (
        '<div class="table-scroll"><table><caption>Harness comparison</caption><thead><tr>'
        '<th scope="col">Harness</th><th scope="col">Attempts</th>'
        '<th scope="col">Successes</th><th scope="col">Success rate</th>'
        '<th scope="col">Completion</th><th scope="col">Mean score / 100</th>'
        '<th scope="col">Score stddev</th><th scope="col">Mean duration</th>'
        '<th scope="col">Median duration</th><th scope="col">Known scores / durations</th>'
        '<th scope="col">Reported usage totals</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _task_info(result: dict, manifest: list) -> tuple[Any, Any]:
    entry = next(
        (
            task
            for task in manifest
            if isinstance(task, dict)
            and task.get("task_id", task.get("id")) == result.get("task_id")
            and (task.get("harness") is None or task.get("harness") == result.get("harness"))
        ),
        {},
    )
    title = result.get("task_title")
    if title is None:
        title = entry.get("task_title", entry.get("title", result.get("task_id")))
    category = result.get("category")
    if category is None:
        category = entry.get("category")
    return title, category if category is not None else "Uncategorized"


def _score_tables(results: list[dict], manifest: list) -> str:
    categories: dict[tuple[str, str], list[dict]] = defaultdict(list)
    tasks: dict[tuple[str, str], list[dict]] = defaultdict(list)
    task_titles: dict[tuple[str, str], str] = {}
    for result in results:
        title, category = _task_info(result, manifest)
        harness_value = result.get("harness")
        harness = str(harness_value) if harness_value is not None else "Unknown"
        task_id = result.get("task_id")
        categories[(harness, str(category))].append(result)
        task_key = (harness, str(task_id) if task_id is not None else "n/a")
        tasks[task_key].append(result)
        task_titles.setdefault(task_key, str(title) if title is not None else "n/a")

    def table(groups: dict, caption: str, headings: list[str]) -> str:
        rows = []
        for labels, group in sorted(groups.items()):
            metrics = _metrics(group)
            rows.append(
                "<tr>"
                + "".join(f"<td>{_text(label)}</td>" for label in labels)
                + f"<td>{metrics['attempts']}</td>"
                + f"<td>{_format(metrics['mean_score'])}</td>"
                + f"<td>{_format(metrics['success_rate'], percent=True)}</td>"
                + f"<td>{metrics['scores_known']}</td></tr>"
            )
        columns = [*headings, "Attempts", "Mean score / 100", "Success rate", "Known scores"]
        if not rows:
            rows.append(
                f'<tr><td colspan="{len(columns)}" class="muted">No attempts recorded.</td></tr>'
            )
        return (
            f'<div class="table-scroll"><table><caption>{caption}</caption><thead><tr>'
            + "".join(f'<th scope="col">{heading}</th>' for heading in columns)
            + "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )

    task_rows = {(*key, task_titles[key]): group for key, group in tasks.items()}
    return table(categories, "Category scores", ["Harness", "Category"]) + table(
        task_rows, "Task scores", ["Harness", "Task ID", "Task"]
    )


def _task_sets(report: dict, summary: dict) -> str:
    manifest = report.get("task_set_manifest")
    if manifest is None:
        return ""
    selected_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{_text(task_set.get(field))}</td>"
            for field in ("id", "title", "description", "task_ids")
        )
        + "</tr>"
        for task_set in manifest
    )
    if not selected_rows:
        selected_rows = '<tr><td colspan="4" class="muted">No task sets selected (direct task selection).</td></tr>'
    task_ids = report.get("resolved_tasks")
    if task_ids is None:
        task_ids = [task_id for task_set in manifest for task_id in task_set.get("task_ids", [])]
        task_ids += [result.get("task_id") for result in report.get("results") or []]
    membership_rows = "".join(
        f"<tr><td>{_text(task_id)}</td><td>"
        + _text(
            [task_set["id"] for task_set in manifest if task_id in task_set.get("task_ids", [])]
        )
        + "</td></tr>"
        for task_id in dict.fromkeys(task_ids)
    )
    if not membership_rows:
        membership_rows = '<tr><td colspan="2" class="muted">No tasks selected.</td></tr>'

    comparison_rows = []
    empty_metrics = _metrics([])
    for task_set in manifest:
        metrics = summary["per_task_set"][task_set["id"]]
        # Include unobserved set/harness pairs for comparison, without adding
        # synthetic observations to the canonical summaries.
        harnesses = summary["per_harness"] or {None: empty_metrics}
        for harness in harnesses:
            group = metrics["per_harness"].get(harness, empty_metrics)
            comparison_rows.append(
                f"<tr><td>{_text(task_set['id'])}</td><td>{_text(task_set.get('title'))}</td>"
                f"<td>{_text(harness)}</td><td>{_text(group['attempts'])}</td>"
                f"<td>{_text(group['successes'])}</td>"
                f"<td>{_format(group['success_rate'], percent=True)}</td>"
                f"<td>{_format(group['mean_score'])}</td>"
                f"<td>{_format(group['mean_duration_seconds'], seconds=True)}</td>"
                f"<td>{_format(group['median_duration_seconds'], seconds=True)}</td></tr>"
            )
    if not comparison_rows:
        comparison_rows.append('<tr><td colspan="9" class="muted">No task sets selected.</td></tr>')
    return (
        "<section><h2>Selected task sets</h2>"
        '<div class="table-scroll"><table><caption>Task-set manifest</caption><thead><tr>'
        '<th scope="col">Task-set ID</th><th scope="col">Title</th>'
        '<th scope="col">Description</th><th scope="col">Task IDs</th>'
        "</tr></thead><tbody>" + selected_rows + "</tbody></table></div>"
        '<div class="table-scroll"><table><caption>Resolved task membership</caption><thead><tr>'
        '<th scope="col">Task ID</th><th scope="col">Selected task sets</th>'
        "</tr></thead><tbody>" + membership_rows + "</tbody></table></div>"
        "<h2>Task-set-by-harness comparison</h2>"
        '<p class="muted">Overlapping task sets include the same attempts in each set; '
        "overall totals count each attempt once.</p>"
        '<div class="table-scroll"><table><caption>Task-set-by-harness comparison</caption>'
        '<thead><tr><th scope="col">Task-set ID</th><th scope="col">Task set</th>'
        '<th scope="col">Harness</th><th scope="col">Attempts</th>'
        '<th scope="col">Successes</th><th scope="col">Success rate</th>'
        '<th scope="col">Mean score / 100</th><th scope="col">Mean duration</th>'
        '<th scope="col">Median duration</th></tr></thead><tbody>'
        + "".join(comparison_rows)
        + "</tbody></table></div></section>"
    )


def _attempt(result: dict, manifest: list, parameters: dict) -> str:
    title, category = _task_info(result, manifest)
    status = result.get("status")
    # Never interpolate an untrusted status into CSS classes.
    status_class = status if status in {"success", "failed", "timeout", "error"} else "unknown"
    termination = result.get("termination") or {}
    warning = termination.get("warning")
    warning_badge = '<span class="badge timeout">Warning</span> ' if warning else ""
    grading = result.get("grading") or {}
    cases = grading.get("cases") or []
    case_rows = (
        "".join(
            f"<tr><td>{_text(case.get('name'))}</td><td>{_text(case.get('status'))}</td>"
            f"<td>{_text(case.get('detail'))}</td></tr>"
            for case in cases
        )
        or '<tr><td colspan="3" class="muted">n/a</td></tr>'
    )
    checks = (
        '<div class="table-scroll"><table><caption>Grading checks</caption><thead><tr>'
        '<th scope="col">Check</th><th scope="col">Status</th><th scope="col">Detail</th>'
        "</tr></thead><tbody>" + case_rows + "</tbody></table></div>"
    )
    patch = result.get("patch") or {}
    logs = result.get("logs") or {}
    log_details = "".join(
        f"<details><summary>{label}</summary>{_pre(logs.get(key))}</details>"
        for key, label in (
            ("stdout", "Agent stdout"),
            ("stderr", "Agent stderr"),
            ("grader_stdout", "Grader stdout"),
            ("grader_stderr", "Grader stderr"),
        )
    )
    return (
        '<details class="attempt"><summary>'
        f'<span class="badge {status_class}">{_text(status)}</span> '
        + warning_badge
        + f"<strong>{_text(result.get('harness'))} · {_text(title)}</strong> "
        f'<span class="muted">Repeat {_text(result.get("repeat"))} · '
        f"Score {_format(result.get('score'))} / 100 · "
        f"{_format(result.get('duration_seconds'), seconds=True)}</span></summary>"
        '<div class="attempt-body">'
        + _pairs(
            {
                "Task ID": result.get("task_id"),
                **({"Task sets": result["task_sets"]} if "task_sets" in result else {}),
                "Category": category,
                "Repeat": result.get("repeat"),
                "Status": status,
                "Agent exit code": result.get("agent_exit_code"),
                "Score": result.get("score"),
                "Duration (seconds)": result.get("duration_seconds"),
            }
        )
        + "<h3>Parameters</h3>"
        + _pairs(parameters)
        + "<h3>Error</h3>"
        + _pre(result.get("error"))
        + checks
        + "<details><summary>Full grading data</summary>"
        + _pre(result.get("grading"))
        + "</details>"
        + "<h3>Final response termination</h3>"
        + _pairs(
            {
                "Stop reason": termination.get("reason"),
                "Final response output tokens": termination.get("output_tokens"),
                "Final response reasoning tokens": termination.get("reasoning_tokens"),
            }
        )
        + (f'<p class="note"><strong>Warning:</strong> {_text(warning)}</p>' if warning else "")
        + "<h3>Usage</h3>"
        + _pre(result.get("usage"))
        + "<h3>Patch</h3>"
        + _pairs(
            {
                "Files changed": patch.get("files_changed"),
                "Lines added": patch.get("lines_added"),
                "Lines removed": patch.get("lines_removed"),
            }
        )
        + "<details><summary>Patch diff</summary>"
        + _pre(patch.get("diff"))
        + "</details>"
        + "<h3>Logs</h3>"
        + log_details
        + "<details><summary>Full attempt JSON</summary><pre>"
        + _json(result)
        + "</pre></details>"
        + "</div></details>"
    )


_CSS = """
:root { color-scheme: dark; --bg:#0c1220; --panel:#151e30; --text:#e8eef8;
  --muted:#adbbd0; --line:#34435b; --accent:#86d5ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1440px; margin:auto; padding:40px 24px 64px; }
header { padding:28px; border:1px solid var(--line); border-radius:16px;
  background:linear-gradient(135deg,#1c304b,var(--panel)); }
h1 { font-size:clamp(1.8rem,4vw,3rem); margin:0; overflow-wrap:anywhere; }
h2 { margin:32px 0 12px; font-size:1.3rem; } h3 { font-size:1rem; margin:20px 0 8px; }
p { margin:8px 0; } .eyebrow { letter-spacing:.18em; color:var(--accent); font-weight:700; }
.muted,dt { color:var(--muted); } .meta { overflow-wrap:anywhere; }
.cards { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:20px 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; }
.card strong { display:block; font-size:1.8rem; color:var(--accent); }
.table-scroll { overflow-x:auto; border:1px solid var(--line); border-radius:12px; margin:16px 0; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
caption { text-align:left; padding:14px 16px; font-weight:700; background:var(--panel); }
th,td { padding:12px 16px; text-align:left; border-top:1px solid var(--line); vertical-align:top; }
th { font-weight:600; } thead { color:var(--muted); background:var(--panel); }
td { overflow-wrap:anywhere; } tbody tr:hover { background:#19253a; }
.usage { margin:0; padding:0; list-style:none; min-width:240px; font-size:.85rem; }
.note { border-left:3px solid var(--accent); background:var(--panel); padding:14px 20px; border-radius:4px; }
details { border:1px solid var(--line); border-radius:10px; margin:10px 0; background:var(--panel); }
summary { cursor:pointer; padding:14px 16px; overflow-wrap:anywhere; }
summary:hover { background:#1b2a40; border-radius:10px; }
summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.attempt-body { padding:0 20px 20px; } .attempt > summary .muted { margin-left:10px; }
.badge { display:inline-block; padding:2px 9px; margin-right:8px; border-radius:6px;
  border:1px solid var(--line); font-size:.8rem; font-weight:700; }
.success { color:#a3e9c1; background:#173c31; } .failed,.error { color:#ffc2c2; background:#47262e; }
.timeout { color:#ffe0a1; background:#44371f; }
.pairs { display:grid; grid-template-columns:minmax(130px,1fr) minmax(0,3fr); gap:6px 16px; }
dt,dd { margin:0; overflow-wrap:anywhere; } dd { white-space:pre-wrap; }
pre { margin:12px 16px; padding:16px; background:#0b1321; border:1px solid var(--line);
  border-radius:8px; white-space:pre-wrap; overflow-wrap:anywhere; overflow:auto;
  max-height:32rem; font:13px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace; }
footer { color:var(--muted); margin-top:32px; }
@media(max-width:700px) { main { padding:16px 12px 40px; } header { padding:20px; }
  .cards { grid-template-columns:repeat(2,minmax(0,1fr)); } .card { padding:12px; }
  .card strong { font-size:1.5rem; } .pairs { grid-template-columns:1fr; }
  dd { margin-bottom:8px; } .attempt-body { padding:0 12px 12px; }
  .attempt > summary .muted { display:block; margin:6px 0 0; } }
@media print { :root { color-scheme:light; --bg:white; --panel:#f3f5f8; --text:#142033;
  --muted:#4b5563; --line:#ccd3de; --accent:#135c85; } header { background:var(--panel); }
  main { padding:0; } pre { background:var(--panel); max-height:none; } }
"""


def render_html(report: dict) -> str:
    """Render an escaped, single-file HTML report with no JavaScript or assets.

    The canonical summary is recalculated from results to keep the comparison
    consistent with attempt details. Original report data is preserved verbatim
    (as escaped JSON) in the full report disclosure.
    """
    results = report.get("results") or []
    summary = summarize(results, report.get("task_set_manifest"))
    model = report.get("model") or {}
    manifest = report.get("task_manifest") or []
    parameters = report.get("parameters") or {}
    cards = "".join(
        f'<div class="card"><span class="muted">{label}</span><strong>{value}</strong></div>'
        for label, value in (
            ("Attempts", _text(summary["attempts"])),
            ("Success rate", _format(summary["success_rate"], percent=True)),
            ("Mean score / 100", _format(summary["mean_score"])),
            ("Median duration", _format(summary["median_duration_seconds"], seconds=True)),
        )
    )
    attempts = "".join(_attempt(result, manifest, parameters) for result in results)
    if not attempts:
        attempts = '<p class="muted">No attempts recorded.</p>'
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>Agent benchmark · {_text(model.get('id'))}</title>"
        "<style>" + _CSS + "</style></head><body><main>"
        '<header><p class="eyebrow">MODEL</p>'
        f"<h1>{_text(model.get('id'))}</h1>"
        f'<p class="meta">Run {_text(report.get("run_id"))} · '
        f"Status {_text(report.get('status'))} · Schema {_text(report.get('schema_version'))}</p>"
        f'<p class="muted meta">Created {_text(report.get("created_at"))} · '
        f"Finished {_text(report.get('finished_at'))}</p>"
        + _pairs(
            {
                "Base URL": model.get("base_url"),
                "Context window": model.get("context_window"),
                "Max tokens": model.get("max_tokens"),
                "Reasoning": model.get("reasoning"),
            }
        )
        + '</header><div class="cards">'
        + cards
        + "</div>"
        '<section aria-label="Methodology" class="note"><strong>How to read this report</strong>'
        "<p>Scores measure correctness, not efficiency: duration, tokens, tool calls, and cost "
        "are reported separately and are not used to weight scores. Repeats are descriptive "
        "observations, not pass@k estimates. All attempts count in the success rate; completion "
        "means success or failed, excluding timeout and error.</p>"
        "<p>Means and medians use known values only. Score standard deviation is the population "
        "standard deviation of known scores. Unknown values display as n/a, not zero. Tokens "
        "and other usage may be unknown; totals sum only reported values, with per-field "
        "coverage shown. Partial totals are not directly comparable to fully observed totals. "
        "Missing categories are shown as Uncategorized.</p>"
        "<p>A final response stopping for length is a token-limit warning (response budget "
        "or server context capacity), not an execution error. Correctness grading and status "
        "are unchanged. Final-response "
        "token counts are separate from aggregate usage; reasoning and output counts may "
        "overlap depending on provider accounting.</p></section>"
        "<section><h2>Harness comparison</h2>"
        + _comparison(summary["per_harness"])
        + "</section>"
        + _task_sets(report, summary)
        + "<section><h2>Category and task scores</h2>"
        + _score_tables(results, manifest)
        + "</section>"
        "<section><h2>Run configuration</h2>"
        "<details><summary>Parameters</summary>" + _pairs(parameters) + "</details>"
        "<details><summary>Environment</summary>" + _pre(report.get("environment")) + "</details>"
        "<details><summary>Task manifest</summary>"
        + _pre(report.get("task_manifest"))
        + "</details>"
        "</section><section><h2>Attempt details</h2>" + attempts + "</section>"
        "<section><h2>Source data</h2><details><summary>Full report JSON</summary><pre>"
        + _json(report)
        + "</pre></details></section>"
        "<footer>Self-contained static report · Works offline and via file:// · No external assets or scripts</footer>"
        "</main></body></html>\n"
    )


def write_reports(report: dict, output: Path) -> tuple[Path, Path]:
    """Write results.json and results.html using atomic replacements.

    The supplied schema and metadata are preserved; ``summary`` is always
    derived from ``results``. Both documents are rendered and staged before
    replacing either destination. Each replacement is atomic, not the pair:
    a crash between replacements can leave different generations of the files.
    Temporary files are cleaned up even if writing or replacement fails.
    """
    output = Path(output)
    normalized = {
        **report,
        "summary": summarize(report.get("results") or [], report.get("task_set_manifest")),
    }
    json_content = json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    html_content = render_html(normalized)
    output.mkdir(parents=True, exist_ok=True)
    paths = (output / "results.json", output / "results.html")
    staged: list[Path] = []
    try:
        for destination, content in zip(paths, (json_content, html_content)):
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=output,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                staged.append(Path(temporary.name))
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
        for temporary, destination in zip(staged, paths):
            os.replace(temporary, destination)
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
    return paths

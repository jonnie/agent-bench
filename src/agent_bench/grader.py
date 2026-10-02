"""Standalone trusted unittest grader.

Usage: python -I /grader/grader.py /candidate /grader/hidden_tests.py
Candidate code executes only in this process. This is output isolation, not a
security sandbox: the runner must impose filesystem/network/resource limits.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from types import TracebackType

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def _format_exception(
    err: tuple[type[BaseException] | None, BaseException | None, TracebackType | None],
) -> str:
    assert err[0] is not None and err[1] is not None
    exception: _ExcInfo = (err[0], err[1], err[2])
    return "".join(traceback.format_exception(*exception))


class CaseResult(unittest.TestResult):
    """One scored case per test method; failing subtests fail their parent case."""

    def __init__(self):
        super().__init__()
        self.cases = []
        self._case = None

    def startTest(self, test):
        super().startTest(test)
        self._case = {"name": test.id(), "status": "passed", "detail": ""}

    def _record(self, status, detail, test=None):
        if self._case is None:
            self.cases.append(
                {
                    "name": test.id() if test is not None else "grader.fixture",
                    "status": status,
                    "detail": detail,
                }
            )
            return
        # An error takes precedence over an assertion failure in another subtest.
        if self._case["status"] != "error":
            self._case["status"] = status
        if detail:
            self._case["detail"] += detail + "\n"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record("failed", _format_exception(err), test)

    def addError(self, test, err):
        super().addError(test, err)
        self._record("error", _format_exception(err), test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            assert err[0] is not None
            status = "failed" if issubclass(err[0], test.failureException) else "error"
            self._record(status, f"{subtest.id()}\n{_format_exception(err)}")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record("failed", f"Skipped (no credit): {reason}", test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record("failed", "Expected failure (no credit):\n" + _format_exception(err))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record("failed", "Unexpected success of an expected-failure test (no credit)")

    def stopTest(self, test):
        assert self._case is not None
        self._case["detail"] = self._case["detail"].rstrip()
        self.cases.append(self._case)
        self._case = None
        super().stopTest(test)


@contextlib.contextmanager
def capture_output():
    """Capture Python streams and direct fd writes, including import-time output."""
    sys.stdout.flush()
    sys.stderr.flush()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    saved_stdout, saved_stderr = os.dup(1), os.dup(2)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as captured:
            os.dup2(captured.fileno(), 1)
            os.dup2(captured.fileno(), 2)
            try:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    yield captured
            finally:
                original_stdout.flush()
                original_stderr.flush()
                captured.flush()
                os.dup2(saved_stdout, 1)
                os.dup2(saved_stderr, 2)
    finally:
        os.close(saved_stdout)
        os.close(saved_stderr)


def _error_case(name, detail):
    return {"name": name, "status": "error", "detail": detail}


def grade(candidate: str, hidden_tests: str) -> dict:
    start = time.perf_counter()
    cases = []
    metrics = None
    with capture_output() as captured:
        try:
            candidate_path = Path(candidate).resolve(strict=True)
            hidden_path = Path(hidden_tests).resolve(strict=True)
            if not candidate_path.is_dir():
                raise ValueError("candidate must be a directory")
            # -I deliberately excludes cwd/PYTHONPATH; explicitly admit only the
            # candidate root. The trusted test module is loaded by absolute path.
            sys.path.insert(0, str(candidate_path))
            spec = importlib.util.spec_from_file_location("_agent_bench_hidden_tests", hidden_path)
            if spec is None or spec.loader is None:
                raise ValueError("hidden_tests must be a loadable Python file")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            if not suite.countTestCases():
                raise ValueError("hidden test suite contains no tests")
            result = CaseResult()
            try:
                suite.run(result)
            finally:
                cases.extend(result.cases)
            possible_metrics = getattr(module, "METRICS", None)
            if isinstance(possible_metrics, dict):
                try:
                    # Snapshot and reject non-JSON values/NaN without losing grades.
                    metrics = json.loads(json.dumps(possible_metrics, allow_nan=False))
                except (TypeError, ValueError):
                    pass
        except BaseException:
            cases.append(_error_case("grader.load_or_run", traceback.format_exc()))
        captured.flush()
        captured.seek(0, io.SEEK_END)
        length = captured.tell()
        captured.seek(max(0, length - 4096))
        output = captured.read()
        if output and any(case["status"] != "passed" for case in cases):
            first_failure = next(case for case in cases if case["status"] != "passed")
            first_failure["detail"] += "\nCaptured output (last 4096 bytes):\n" + output
    total = len(cases)
    passed = sum(case["status"] == "passed" for case in cases)
    failed = sum(case["status"] == "failed" for case in cases)
    errors = sum(case["status"] == "error" for case in cases)
    report = {
        "tests_total": total,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_errors": errors,
        "score": round(100 * passed / total, 2) if total else 0.0,
        "success": total > 0 and passed == total,
        "cases": cases,
        "duration_seconds": time.perf_counter() - start,
    }
    if metrics is not None:
        report["metrics"] = metrics
    return report


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    sys.stdout.flush()
    sys.stderr.flush()
    # Keep a private report channel and leave ordinary fds muted through process
    # exit, so candidate atexit callbacks cannot append text after the JSON.
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as report_output:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        if len(args) == 2:
            report = grade(*args)
        else:
            report = {
                "tests_total": 1,
                "tests_passed": 0,
                "tests_failed": 0,
                "tests_errors": 1,
                "score": 0.0,
                "success": False,
                "cases": [
                    _error_case("grader.arguments", "Usage: grader.py CANDIDATE HIDDEN_TESTS")
                ],
                "duration_seconds": 0.0,
            }
        # Candidate failures are represented in JSON, not the exit status.
        report_output.write(json.dumps(report, allow_nan=False) + "\n")
        report_output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

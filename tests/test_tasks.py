"""Trusted framework tests: candidate modules run only in grader subprocesses."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from agent_bench.tasks import TASKS, Task, get_tasks

# Golden implementations are fixture data, never imported by this process.
GOLDEN = {
    "bug-fix": {
        "solution/billing.py": """
            from decimal import Decimal, ROUND_HALF_EVEN

            def invoice_total(items):
                total = sum((Decimal(item['quantity']) * Decimal(item['unit_price']) *
                             (1 - Decimal(item.get('discount_pct', '0')) / 100)
                             for item in items), Decimal(0))
                total = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
                return format(total, '.2f') if total else '0.00'
        """,
    },
    "small-feature": {
        "solution/settings.py": """
            from copy import deepcopy

            DELETE = object()

            def merge_settings(base, override):
                result = deepcopy(base)
                for key, value in override.items():
                    if value is DELETE:
                        result.pop(key, None)
                    elif isinstance(value, dict):
                        old = base.get(key)
                        result[key] = merge_settings(old if isinstance(old, dict) else {}, value)
                    else:
                        result[key] = deepcopy(value)
                return result
        """,
    },
    "system-refactor": {
        "solution/pricing.py": """
            from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

            CENT = Decimal('0.01')

            def line_total(quantity, unit_price, discount_pct='0'):
                if type(quantity) is not int or quantity < 0:
                    raise ValueError('quantity must be a nonnegative integer')
                if not isinstance(unit_price, str) or not isinstance(discount_pct, str):
                    raise ValueError('price and discount must be decimal strings')
                try:
                    price, discount = Decimal(unit_price), Decimal(discount_pct)
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError('invalid decimal') from exc
                if not price.is_finite() or not discount.is_finite():
                    raise ValueError('non-finite monetary value')
                if price < 0 or not 0 <= discount <= 100:
                    raise ValueError('price or discount outside valid range')
                return (quantity * price * (1 - discount / 100)).quantize(
                    CENT, rounding=ROUND_HALF_EVEN)
        """,
        "solution/checkout.py": """
            from decimal import Decimal
            from . import pricing

            def order_total(lines):
                return sum((pricing.line_total(line['quantity'], line['unit_price'],
                                               line.get('discount_pct', '0'))
                            for line in lines), Decimal('0.00'))
        """,
        "solution/reports.py": """
            from decimal import Decimal
            from . import pricing

            def total_by_sku(lines):
                totals = {}
                for line in lines:
                    amount = pricing.line_total(line['quantity'], line['unit_price'],
                                                line.get('discount_pct', '0'))
                    sku = line['sku']
                    totals[sku] = totals.get(sku, Decimal('0.00')) + amount
                return totals
        """,
    },
    "security-hardening": {
        "solution/notes.py": """
            import errno
            import os
            import re
            import stat

            def read_note(root, name):
                if not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) is None:
                    raise ValueError('invalid note name')
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    try:
                        fd = os.open(name + '.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
                    except OSError as exc:
                        if exc.errno == errno.ELOOP:
                            raise ValueError('symlink note') from exc
                        raise
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ValueError('note must be a regular file')
                        # closefd=False keeps ownership in this finally block.
                        with os.fdopen(fd, 'r', encoding='utf-8', newline='', closefd=False) as stream:
                            return stream.read()
                    finally:
                        os.close(fd)
                finally:
                    os.close(directory)
        """,
    },
    "performance": {
        "solution/windows.py": """
            def rolling_counts(timestamps, window):
                result = []
                left = 0
                for index in range(len(timestamps)):
                    current = timestamps[index]
                    while left <= index and timestamps[left] <= current - window:
                        left += 1
                    result.append(index - left + 1)
                return result
        """,
    },
}


def source(text):
    return dedent(text).lstrip("\n")


class SubprocessGraderTests(unittest.TestCase):
    def run_grader(self, files, hidden_tests):
        with tempfile.TemporaryDirectory(prefix="agent-bench-test-") as directory:
            root = Path(directory)
            candidate, grader = root / "candidate", root / "grader"
            candidate.mkdir()
            grader.mkdir()
            for name, contents in files.items():
                path = candidate / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            shutil.copyfile(PROJECT / "src/agent_bench/grader.py", grader / "grader.py")
            (grader / "hidden_tests.py").write_text(source(hidden_tests), encoding="utf-8")
            # A poisoned cwd package verifies -I and the explicit candidate path.
            (root / "solution").mkdir()
            (root / "solution/__init__.py").write_text(
                "raise RuntimeError('cwd imported')\n", encoding="utf-8"
            )
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(grader / "grader.py"),
                    str(candidate),
                    str(grader / "hidden_tests.py"),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "", process.stderr)
        # json.loads on the entire stdout rejects log prefixes or extra JSON.
        report = json.loads(process.stdout)
        self.assertEqual(len(process.stdout.splitlines()), 1)
        self.assertEqual(report["tests_total"], len(report["cases"]))
        self.assertEqual(
            report["tests_total"],
            report["tests_passed"] + report["tests_failed"] + report["tests_errors"],
        )
        self.assertGreaterEqual(report["duration_seconds"], 0)
        self.assertGreaterEqual(report["score"], 0)
        self.assertLessEqual(report["score"], 100)
        self.assertEqual(
            report["success"],
            report["tests_total"] > 0 and report["tests_passed"] == report["tests_total"],
        )
        for case in report["cases"]:
            self.assertEqual(set(case), {"name", "status", "detail"})
            self.assertIn(case["status"], {"passed", "failed", "error"})
            self.assertIsInstance(case["detail"], str)
        return report

    def test_every_baseline_fails_hidden_contract(self):
        for task in TASKS.values():
            with self.subTest(task=task.id):
                report = self.run_grader(task.files, task.hidden_tests)
                self.assertFalse(report["success"], report)
                self.assertLess(report["score"], 100)
                self.assertGreater(report["tests_failed"] + report["tests_errors"], 0)
                # Baselines must have real behavioral passes, not just be broken imports.
                self.assertGreater(report["tests_passed"], 0, report)

    def test_every_golden_passes_hidden_and_public_tests(self):
        for task in TASKS.values():
            with self.subTest(task=task.id):
                files = dict(task.files)
                files.update({name: source(code) for name, code in GOLDEN[task.id].items()})
                report = self.run_grader(files, task.hidden_tests)
                self.assertTrue(report["success"], report)
                self.assertEqual(report["score"], 100)
                # Load the public test module under the same isolated grader.
                public_report = self.run_grader(files, files["tests/test_public.py"])
                self.assertTrue(public_report["success"], public_report)
                if task.id == "performance":
                    self.assertEqual(set(report["metrics"]), {"all_equal", "expiring", "wide"})
                    for metric in report["metrics"].values():
                        self.assertLessEqual(metric["element_reads"], 12 * metric["n"] + 100)

    def test_refactor_rejects_duplicate_behaviorally_correct_clients(self):
        task = TASKS["system-refactor"]
        files = dict(task.files)
        files["solution/pricing.py"] = source(GOLDEN[task.id]["solution/pricing.py"])
        report = self.run_grader(files, task.hidden_tests)
        architecture = next(
            case
            for case in report["cases"]
            if case["name"].endswith("test_architecture_not_just_behavior")
        )
        self.assertEqual(architecture["status"], "failed")
        delegation = next(
            case for case in report["cases"] if case["name"].endswith("test_dynamic_delegation")
        )
        self.assertEqual(delegation["status"], "failed")

    def test_import_and_test_output_cannot_corrupt_json(self):
        files = {
            "solution/__init__.py": source("""
            import atexit
            import os
            import sys
            atexit.register(print, 'candidate exit output')
            atexit.register(os.write, 2, b'candidate exit stderr\\n')
            print('candidate import output')
            print('original stdout output', file=sys.__stdout__)
            print('original stderr output', file=sys.__stderr__)
            os.write(1, b'direct stdout output\\n')
            os.write(2, b'direct stderr output\\n')
        """)
        }
        report = self.run_grader(
            files,
            """
            import unittest
            import solution
            print('hidden import output')
            METRICS = {'work': 12}
            class Noise(unittest.TestCase):
                def test_print(self):
                    print('test output')
                    self.assertTrue(True)
        """,
        )
        self.assertTrue(report["success"], report)
        self.assertEqual(report["metrics"], {"work": 12})

    def test_result_accounting_subtests_skips_and_errors(self):
        report = self.run_grader(
            {},
            """
            import unittest
            class Outcomes(unittest.TestCase):
                def test_pass(self):
                    pass
                def test_fail(self):
                    self.fail('assertion detail')
                def test_error(self):
                    raise RuntimeError('exception detail')
                @unittest.skip('not credit')
                def test_skip(self):
                    pass
                @unittest.expectedFailure
                def test_expected_failure(self):
                    self.fail('expected')
                @unittest.expectedFailure
                def test_unexpected_success(self):
                    pass
                def test_subtests(self):
                    for i in range(3):
                        with self.subTest(i=i):
                            self.assertEqual(i, 0)
                def test_subtest_error_precedes_failure(self):
                    with self.subTest(kind='error'):
                        raise RuntimeError('subtest exception')
                    with self.subTest(kind='failure'):
                        self.fail('subtest assertion')
        """,
        )
        self.assertEqual(report["tests_total"], 8)
        self.assertEqual(report["tests_passed"], 1)
        self.assertEqual(report["tests_failed"], 5)
        self.assertEqual(report["tests_errors"], 2)
        self.assertEqual(report["score"], 12.5)
        failure = next(case for case in report["cases"] if case["name"].endswith("test_fail"))
        self.assertIn("assertion detail", failure["detail"])

    def test_import_failure_and_system_exit_are_json_errors(self):
        for failure in ["raise RuntimeError('cannot import')", "raise SystemExit(7)"]:
            with self.subTest(failure=failure):
                report = self.run_grader({}, failure)
                self.assertEqual(report["tests_errors"], 1)
                self.assertEqual(report["score"], 0)
                self.assertFalse(report["success"])

    def test_fixture_error_and_empty_suite(self):
        report = self.run_grader(
            {},
            """
            import unittest
            class BrokenFixture(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise RuntimeError('setup failed')
                def test_never_runs(self):
                    pass
        """,
        )
        self.assertEqual(report["tests_errors"], 1)
        self.assertIn("setup failed", report["cases"][0]["detail"])
        report = self.run_grader({}, "import unittest\n")
        self.assertFalse(report["success"])
        self.assertIn("no tests", report["cases"][0]["detail"])

    def test_invalid_metrics_do_not_destroy_valid_results(self):
        report = self.run_grader(
            {},
            """
            import unittest
            METRICS = {'invalid': float('nan')}
            class Valid(unittest.TestCase):
                def test_ok(self):
                    pass
        """,
        )
        self.assertTrue(report["success"], report)
        self.assertNotIn("metrics", report)


class TaskApiTests(unittest.TestCase):
    def test_catalog_and_candidate_layout(self):
        self.assertEqual(
            set(TASKS),
            {"bug-fix", "small-feature", "system-refactor", "security-hardening", "performance"},
        )
        for task_id, task in TASKS.items():
            with self.subTest(task=task_id):
                self.assertIsInstance(task, Task)
                self.assertEqual(task.id, task_id)
                self.assertEqual(task.category, task_id)
                self.assertTrue(task.title)
                self.assertTrue(task.prompt)
                self.assertIn(task.prompt, task.files["README.md"])
                self.assertIn("solution/__init__.py", task.files)
                self.assertIn("tests/test_public.py", task.files)
                self.assertNotIn("hidden_tests.py", task.files)
                for name, contents in task.files.items():
                    self.assertFalse(Path(name).is_absolute())
                    self.assertNotIn("..", Path(name).parts)
                    self.assertIsInstance(contents, str)
                    self.assertTrue(name == "README.md" or name.startswith(("solution/", "tests/")))
                self.assertIn("unittest", task.hidden_tests)
                compile(task.hidden_tests, "<trusted hidden test fixture>", "exec")
        modules = [name for name in TASKS["system-refactor"].files if name.startswith("solution/")]
        self.assertGreaterEqual(len(modules), 4)

    def test_lookup_order_duplicates_empty_and_unknown(self):
        self.assertEqual(get_tasks([]), [])
        ids = ["performance", "bug-fix", "performance"]
        self.assertEqual([task.id for task in get_tasks(ids)], ids)
        self.assertIs(get_tasks(["bug-fix"])[0], TASKS["bug-fix"])
        with self.assertRaises(KeyError):
            get_tasks(["unknown"])

    def test_metadata_defaults_are_independent(self):
        first = Task("a", "A", "bug-fix", "", {}, "")
        second = Task("b", "B", "bug-fix", "", {}, "")
        first.metadata["custom"] = True
        self.assertEqual(second.metadata, {})


if __name__ == "__main__":
    unittest.main()

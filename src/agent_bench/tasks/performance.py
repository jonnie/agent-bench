"""Built-in performance benchmark fixture. Candidate source is data."""

from .base import create_task

TASK = create_task(
    "performance",
    "Make rolling event counts linear",
    """
        Optimize solution.windows.rolling_counts(timestamps, window) for a service
        dashboard. timestamps is a nondecreasing finite Sequence of integer
        timestamps; duplicates are valid. window is a positive integer. Inputs
        are valid. Return a list where result[i] counts indices j <= i satisfying
        timestamps[i] - window < timestamps[j] <= timestamps[i]. Thus the left
        boundary is EXCLUSIVE, and future equal-timestamp events do not count.
        Empty input returns []. Preserve the input, signature, and semantics.
        Use O(n) time, O(1) auxiliary state excluding the returned O(n) list.
        Support read-only Sequence objects, not just lists. Hidden tests count
        element reads (slices charged by their length): <= 12*n + 100 reads on
        deterministic adversarial inputs. They also enforce a generous 3-second
        bound for 4,000 events; work count, not machine speed, is the primary
        complexity check. Do not sort, repeatedly slice, or scan each prefix.
        Run public tests with python -m unittest discover -s tests.
        """,
    {
        "windows.py": """
            def rolling_counts(timestamps, window):
                result = []
                for index, current in enumerate(timestamps):
                    result.append(sum(1 for value in timestamps[:index + 1]
                                      if current - window < value <= current))
                return result
        """
    },
    """
        import unittest
        from solution.windows import rolling_counts

        class WindowTests(unittest.TestCase):
            def test_boundaries_and_duplicates(self):
                self.assertEqual(rolling_counts([0, 0, 2, 3, 5], 3), [1, 2, 3, 2, 2])
                self.assertEqual(rolling_counts([], 5), [])
        """,
    """
        import random
        import time
        import unittest
        from collections.abc import Sequence
        from solution.windows import rolling_counts

        METRICS = {}

        class ReadCounter(Sequence):
            def __init__(self, values):
                self.values = tuple(values)
                self.reads = 0
            def __len__(self):
                return len(self.values)
            def __getitem__(self, index):
                if isinstance(index, slice):
                    values = self.values[index]
                    self.reads += len(values)
                    return values
                self.reads += 1
                return self.values[index]

        def reference(values, window):
            return [sum(current - window < value <= current for value in values[:i + 1])
                    for i, current in enumerate(values)]

        class WindowContract(unittest.TestCase):
            def test_regressions(self):
                self.assertEqual(rolling_counts([], 1), [])
                self.assertEqual(rolling_counts([0, 0, 2, 3, 5], 3), [1, 2, 3, 2, 2])
                self.assertEqual(rolling_counts([-4, -3, -3, 0], 1), [1, 1, 2, 1])
                self.assertEqual(rolling_counts([7] * 10, 4), list(range(1, 11)))

            def test_seeded_correctness_and_input_preservation(self):
                rng = random.Random(90381)
                for _ in range(90):
                    values = sorted(rng.randint(-100, 100) for _ in range(rng.randrange(80)))
                    before = values.copy()
                    window = rng.randrange(1, 101)
                    self.assertEqual(rolling_counts(values, window), reference(values, window))
                    self.assertEqual(values, before)
                    self.assertEqual(rolling_counts(tuple(values), window), reference(values, window))

            def test_linear_work_and_generous_runtime(self):
                size = 4000
                patterns = [('all_equal', [10] * size, 5, list(range(1, size + 1))),
                            ('expiring', list(range(size)), 1, [1] * size),
                            ('wide', list(range(size)), size + 1, list(range(1, size + 1)))]
                for name, values, window, expected in patterns:
                    with self.subTest(pattern=name):
                        counted = ReadCounter(values)
                        start = time.perf_counter()
                        actual = rolling_counts(counted, window)
                        elapsed = time.perf_counter() - start
                        METRICS[name] = {'element_reads': counted.reads, 'seconds': elapsed, 'n': size}
                        self.assertEqual(actual, expected)
                        self.assertLessEqual(counted.reads, 12 * size + 100, 'quadratic sequence work')
                        self.assertLess(elapsed, 3.0, 'generous runtime ceiling exceeded')
        """,
    {"seed": 90381, "max_element_reads_per_item": 12, "runtime_limit_seconds": 3.0},
)

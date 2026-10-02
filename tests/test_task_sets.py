"""Task selection contracts and pre-migration fixture preservation hashes."""

import ast
import dataclasses
import hashlib
import importlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from agent_bench.task_sets import (
    DEFAULT_TASK_SETS,
    TASK_SETS,
    TaskSet,
    get_task_sets,
    resolve_tasks,
)
from agent_bench.task_sets import _build_registry as build_task_set_registry
from agent_bench.tasks import TASKS, Task, get_tasks
from agent_bench.tasks import _build_registry as build_task_registry
from agent_bench.tasks.base import create_task

TASK_IDS = ("bug-fix", "small-feature", "system-refactor", "security-hardening", "performance")
# Captured from the original tasks.py before extraction; JSON retains dictionary order.
TASK_HASHES = {
    "bug-fix": "2261b7919f0f20e0e31d72bfca284334ef1770ff6a9b1520c60bdd39124c2eec",
    "small-feature": "7f7f90052c6d9b63b63ba2408f0474c6346835a07c5e003bdff46dfee4e73c5e",
    "system-refactor": "eb259d7e0491f171e0bdee2277e9ec1eb0646006c2acdc67d03cbe92d4d2ec9a",
    "security-hardening": "1bee1367af20dcc79bfa5595f8bec12c8a9b5a41755581a9d9007832c1697d66",
    "performance": "34ffb30f0bbcda4b405a5485fbe854f273667d88b301948f7e409bd105b48528",
}
CATALOG_HASH = "ff3e2afabc7eda5269af9922ba367d17b61bc389ce2ff27d62f46ba95ebf8303"
FIELD_HASHES = {
    "prompt": "714f04d63c4bec88ec3ce179de7582ba98d7f64da520eb7563ce605f41239cb1",
    "files": "c865d0e9b5332e85e3f6ae4179349d67f0d203f1a647af3ab4352ffaf82301e9",
    "hidden_tests": "2682927e0da6a428f720e73a0d53b683462531c3b5cc05c636a3e7c8e82cba01",
    "metadata": "b16172dc6ec05cb12e5f650f8e6dadbde67116d292a4ae5f316b3325a425a702",
}


def data_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def task_ids(tasks):
    return [task.id for task in tasks]


class TaskSetSelectionTests(unittest.TestCase):
    def test_builtin_sets_and_dataclass(self):
        self.assertEqual(tuple(TASK_SETS), ("default", "core", "robustness"))
        self.assertEqual(DEFAULT_TASK_SETS, ("default",))
        self.assertEqual(TASK_SETS["default"].task_ids, TASK_IDS)
        self.assertEqual(TASK_SETS["core"].task_ids, TASK_IDS[:3])
        self.assertEqual(TASK_SETS["robustness"].task_ids, TASK_IDS[3:])
        self.assertEqual(
            [field.name for field in dataclasses.fields(TaskSet)],
            ["id", "title", "description", "task_ids"],
        )
        self.assertEqual(dataclasses.fields(TaskSet)[3].type, tuple[str, ...])
        for name, task_set in TASK_SETS.items():
            with self.subTest(task_set=name):
                self.assertIsInstance(task_set, TaskSet)
                self.assertEqual(task_set.id, name)
                self.assertTrue(task_set.title)
                self.assertTrue(task_set.description)
                self.assertIsInstance(task_set.task_ids, tuple)

    def test_default_only_when_both_unspecified(self):
        self.assertEqual(task_ids(resolve_tasks()), list(TASK_IDS))
        self.assertEqual(task_ids(resolve_tasks(tasks=None, task_sets=None)), list(TASK_IDS))
        self.assertEqual(task_ids(resolve_tasks(tasks=["performance"])), ["performance"])
        self.assertEqual(task_ids(resolve_tasks(task_sets=["core"])), list(TASK_IDS[:3]))
        self.assertEqual(task_ids(resolve_tasks(task_sets=["robustness"])), list(TASK_IDS[3:]))
        self.assertEqual(task_ids(resolve_tasks(task_sets=["default"])), list(TASK_IDS))

    def test_explicit_tasks_keep_order_and_identity(self):
        selected = resolve_tasks(tasks=["performance", "bug-fix"])
        self.assertEqual(task_ids(selected), ["performance", "bug-fix"])
        self.assertIs(selected[0], TASKS["performance"])
        self.assertIs(selected[1], TASKS["bug-fix"])

    def test_set_union_and_first_occurrence_order(self):
        cases = [
            (["core", "robustness"], None, list(TASK_IDS)),
            (["robustness", "core"], None, list(TASK_IDS[3:] + TASK_IDS[:3])),
            (["core", "default"], None, list(TASK_IDS)),
            (["robustness", "default", "core"], None, list(TASK_IDS[3:] + TASK_IDS[:3])),
            (["core"], ["performance", "bug-fix"], list(TASK_IDS[:3]) + ["performance"]),
            (
                ["robustness"],
                ["performance", "small-feature", "bug-fix"],
                ["security-hardening", "performance", "small-feature", "bug-fix"],
            ),
        ]
        for sets, tasks, expected in cases:
            with self.subTest(sets=sets, tasks=tasks):
                self.assertEqual(task_ids(resolve_tasks(tasks=tasks, task_sets=sets)), expected)

    def test_empty_selections_do_not_fall_back_to_defaults(self):
        for selection in ({"tasks": []}, {"task_sets": []}, {"tasks": [], "task_sets": []}):
            with (
                self.subTest(selection=selection),
                self.assertRaisesRegex(ValueError, "at least one"),
            ):
                resolve_tasks(**selection)
        self.assertEqual(task_ids(resolve_tasks(tasks=[], task_sets=["core"])), list(TASK_IDS[:3]))
        self.assertEqual(task_ids(resolve_tasks(tasks=["bug-fix"], task_sets=[])), ["bug-fix"])

    def test_selection_types(self):
        invalid = (
            "core",
            b"core",
            ("core",),
            {"core"},
            {"core": True},
            0,
            False,
            [1],
            [None],
            [True],
            [b"core"],
            [["core"]],
            ["core", 1],
        )
        for argument in ("tasks", "task_sets"):
            for value in invalid:
                with self.subTest(argument=argument, value=value):
                    with self.assertRaisesRegex(
                        ValueError, argument + " must be a list of strings"
                    ):
                        resolve_tasks(**{argument: cast(Any, value)})
        with self.assertRaisesRegex(ValueError, "list of strings"):
            resolve_tasks(tasks=[], task_sets=cast(Any, (name for name in ["core"])))
        with self.assertRaises(TypeError):
            cast(Any, resolve_tasks)(["bug-fix"])

    def test_unknown_names_are_readable_value_errors(self):
        for selection, kind in (
            ({"tasks": ["missing-task"]}, "tasks"),
            ({"task_sets": ["missing-set"]}, "task_sets"),
            ({"tasks": ["core"]}, "tasks"),
            ({"task_sets": ["bug-fix"]}, "task_sets"),
            ({"tasks": [""], "task_sets": ["default"]}, "tasks"),
        ):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, "Unknown.*" + kind):
                    resolve_tasks(**selection)

    def test_duplicate_names_rejected_even_with_overlap(self):
        for selection in (
            {"tasks": ["bug-fix", "bug-fix"]},
            {"tasks": ["bug-fix", "performance", "bug-fix"], "task_sets": ["default"]},
            {"task_sets": ["core", "core"]},
            {"task_sets": ["core", "robustness", "core"], "tasks": ["bug-fix"]},
        ):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "Duplicate"):
                resolve_tasks(**selection)

    def test_get_task_sets_lookup_validation(self):
        self.assertEqual(get_task_sets([]), [])
        selected = get_task_sets(["robustness", "core"])
        self.assertEqual([task_set.id for task_set in selected], ["robustness", "core"])
        self.assertIs(selected[0], TASK_SETS["robustness"])
        with self.assertRaisesRegex(ValueError, "Unknown.*missing"):
            get_task_sets(["missing"])
        with self.assertRaisesRegex(ValueError, "Duplicate.*core"):
            get_task_sets(["core", "core"])
        for value in (None, "core", ("core",), [None], [1]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "list of strings"):
                get_task_sets(cast(Any, value))

    def test_selection_does_not_mutate_input_lists(self):
        tasks, sets = ["performance", "bug-fix"], ["core", "default"]
        resolve_tasks(tasks=tasks, task_sets=sets)
        self.assertEqual(tasks, ["performance", "bug-fix"])
        self.assertEqual(sets, ["core", "default"])
        self.assertIsNot(resolve_tasks(), resolve_tasks())

    def test_explicit_membership_not_category_inference(self):
        variant = create_task("invoice-variant", "Variant", "", {}, "", "", category="bug-fix")
        variant_set = TaskSet("variants", "Variants", "Explicit variant group", (variant.id,))
        with (
            patch.dict(TASKS, {variant.id: variant}),
            patch.dict(TASK_SETS, {"variants": variant_set}),
        ):
            self.assertEqual(resolve_tasks(task_sets=["variants"]), [variant])
            self.assertEqual(resolve_tasks(tasks=[variant.id]), [variant])
            self.assertNotIn(variant, resolve_tasks(task_sets=["core"]))


class RegistryValidationTests(unittest.TestCase):
    def test_duplicate_task_ids(self):
        task = TASKS["bug-fix"]
        other = dataclasses.replace(task, title="Different task, same ID")
        with self.assertRaisesRegex(ValueError, "Duplicate task ID.*bug-fix"):
            build_task_registry((task, other))
        self.assertEqual(tuple(build_task_registry(tuple(TASKS.values()))), TASK_IDS)

    def test_duplicate_set_ids(self):
        first = TASK_SETS["core"]
        other = dataclasses.replace(first, task_ids=("performance",))
        with self.assertRaisesRegex(ValueError, "Duplicate task set ID.*core"):
            build_task_set_registry((first, other))

    def test_empty_unknown_and_duplicate_members(self):
        invalid = (
            ((), "must not be empty"),
            (("missing-task",), "Unknown task ID.*missing-task.*invalid"),
            (("bug-fix", "performance", "bug-fix"), "Duplicate task ID.*bug-fix.*invalid"),
        )
        for members, message in invalid:
            with self.subTest(members=members), self.assertRaisesRegex(ValueError, message):
                build_task_set_registry((TaskSet("invalid", "Invalid", "Invalid set", members),))
        self.assertEqual(
            tuple(build_task_set_registry(tuple(TASK_SETS.values()))),
            ("default", "core", "robustness"),
        )


class TaskPackageTests(unittest.TestCase):
    def test_original_data_and_order_hashes(self):
        self.assertEqual(tuple(TASKS), TASK_IDS)
        for name, task in TASKS.items():
            with self.subTest(task=name):
                self.assertEqual(data_hash(dataclasses.asdict(task)), TASK_HASHES[name])
        self.assertEqual(
            data_hash([dataclasses.asdict(task) for task in TASKS.values()]), CATALOG_HASH
        )
        for field, expected in FIELD_HASHES.items():
            with self.subTest(field=field):
                self.assertEqual(
                    data_hash([getattr(task, field) for task in TASKS.values()]), expected
                )

    def test_module_separation_and_public_exports(self):
        package = importlib.import_module("agent_bench.tasks")
        self.assertEqual(package.__all__, ["TASKS", "Task", "get_tasks"])
        assert package.__file__ is not None
        self.assertEqual(Path(package.__file__).name, "__init__.py")
        self.assertFalse((PROJECT / "src/agent_bench/tasks.py").exists())
        self.assertIs(Task, importlib.import_module("agent_bench.tasks.base").Task)
        for name in TASK_IDS:
            module = importlib.import_module("agent_bench.tasks." + name.replace("-", "_"))
            self.assertIs(module.TASK, TASKS[name])
            assert module.__file__ is not None
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            self.assertEqual(len(calls), 1)
            func = calls[0].func
            assert isinstance(func, ast.Name)
            self.assertEqual(func.id, "create_task")
        for name in TASK_SETS:
            module = importlib.import_module("agent_bench.task_sets." + name)
            self.assertIs(module.TASK_SET, TASK_SETS[name])

    def test_legacy_lookup_semantics(self):
        self.assertEqual(get_tasks([]), [])
        self.assertEqual(
            task_ids(get_tasks(["performance", "bug-fix", "performance"])),
            ["performance", "bug-fix", "performance"],
        )
        self.assertIs(get_tasks(["bug-fix"])[0], TASKS["bug-fix"])
        with self.assertRaises(KeyError) as error:
            get_tasks(["missing"])
        self.assertEqual(error.exception.args, ("missing",))

    def test_factory_category_and_fixture_normalization(self):
        args = (
            "variant",
            "Variant title",
            "\n    Prompt\n",
            {"module.py": "\n    CODE\n"},
            "\n    PUBLIC\n",
            "\n    HIDDEN\n",
        )
        default = create_task(*args)
        variant = create_task(*args, category="bug-fix")
        self.assertEqual(default.category, "variant")
        self.assertEqual(variant.category, "bug-fix")
        self.assertEqual(variant.id, "variant")
        self.assertEqual(variant.prompt, "Prompt\n")
        self.assertEqual(variant.hidden_tests, "HIDDEN\n")
        self.assertEqual(
            variant.files,
            {
                "solution/__init__.py": "",
                "README.md": "# Variant title\n\nPrompt\n",
                "solution/module.py": "CODE\n",
                "tests/test_public.py": "PUBLIC\n",
            },
        )
        self.assertEqual(create_task(*args, category=None).category, "variant")
        self.assertEqual(create_task(*args, category="").category, "")
        metadata = {"seed": 123}
        self.assertIs(create_task(*args, metadata).metadata, metadata)
        default.metadata["custom"] = True
        self.assertEqual(variant.metadata, {})
        first = Task("a", "A", "bug-fix", "", {}, "")
        second = Task("b", "B", "bug-fix", "", {}, "")
        first.metadata["custom"] = True
        self.assertEqual(second.metadata, {})


if __name__ == "__main__":
    unittest.main()

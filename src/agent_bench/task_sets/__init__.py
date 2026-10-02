"""Explicit task-set registry and validated, ordered task selection."""

from ..tasks import TASKS, Task
from .base import TaskSet
from .core import TASK_SET as CORE
from .default import TASK_SET as DEFAULT
from .robustness import TASK_SET as ROBUSTNESS

__all__ = ["TASK_SETS", "DEFAULT_TASK_SETS", "TaskSet", "get_task_sets", "resolve_tasks"]


def _build_registry(task_sets: tuple[TaskSet, ...]) -> dict[str, TaskSet]:
    registry = {}
    for task_set in task_sets:
        if task_set.id in registry:
            raise ValueError(f"Duplicate task set ID: {task_set.id!r}")
        if not task_set.task_ids:
            raise ValueError(f"Task set {task_set.id!r} must not be empty")
        seen = set()
        for task_id in task_set.task_ids:
            if task_id not in TASKS:
                raise ValueError(f"Unknown task ID {task_id!r} in task set {task_set.id!r}")
            if task_id in seen:
                raise ValueError(f"Duplicate task ID {task_id!r} in task set {task_set.id!r}")
            seen.add(task_id)
        registry[task_set.id] = task_set
    return registry


TASK_SETS: dict[str, TaskSet] = _build_registry((DEFAULT, CORE, ROBUSTNESS))
DEFAULT_TASK_SETS = ("default",)


def _validate_ids(ids: list[str], *, kind: str, registry: dict) -> None:
    if not isinstance(ids, list) or any(not isinstance(name, str) for name in ids):
        raise ValueError(f"{kind} must be a list of strings")
    seen = set()
    for name in ids:
        if name in seen:
            raise ValueError(f"Duplicate name in {kind}: {name!r}")
        if name not in registry:
            raise ValueError(f"Unknown name in {kind}: {name!r}")
        seen.add(name)


def get_task_sets(ids: list[str]) -> list[TaskSet]:
    """Return named task sets in order, rejecting invalid or duplicate names."""
    _validate_ids(ids, kind="task_sets", registry=TASK_SETS)
    return [TASK_SETS[task_set_id] for task_set_id in ids]


def resolve_tasks(
    *, tasks: list[str] | None = None, task_sets: list[str] | None = None
) -> list[Task]:
    """Expand sets first, then explicit tasks, deduplicating in first-occurrence order.

    Defaults apply only when both arguments are unspecified. Explicit selections
    must select at least one task.
    """
    if tasks is None and task_sets is None:
        task_sets = list(DEFAULT_TASK_SETS)
    if tasks is not None:
        _validate_ids(tasks, kind="tasks", registry=TASKS)
    selected_sets = get_task_sets(task_sets) if task_sets is not None else []
    selected = {}
    for task_set in selected_sets:
        for task_id in task_set.task_ids:
            selected.setdefault(task_id, TASKS[task_id])
    for task_id in tasks if tasks is not None else []:
        selected.setdefault(task_id, TASKS[task_id])
    if not selected:
        raise ValueError(
            "Select at least one task or task set; explicit empty selections are invalid"
        )
    return list(selected.values())

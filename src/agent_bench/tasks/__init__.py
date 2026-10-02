"""Explicit benchmark task registry. Candidate source is data, never imported here."""

from .base import Task
from .bug_fix import TASK as BUG_FIX
from .performance import TASK as PERFORMANCE
from .security_hardening import TASK as SECURITY_HARDENING
from .small_feature import TASK as SMALL_FEATURE
from .system_refactor import TASK as SYSTEM_REFACTOR

__all__ = ["TASKS", "Task", "get_tasks"]


def _build_registry(tasks: tuple[Task, ...]) -> dict[str, Task]:
    registry = {}
    for task in tasks:
        if task.id in registry:
            raise ValueError(f"Duplicate task ID: {task.id!r}")
        registry[task.id] = task
    return registry


TASKS: dict[str, Task] = _build_registry(
    (BUG_FIX, SMALL_FEATURE, SYSTEM_REFACTOR, SECURITY_HARDENING, PERFORMANCE)
)


def get_tasks(ids: list[str]) -> list[Task]:
    """Return tasks in requested order, preserving duplicates; unknown IDs raise KeyError."""
    return [TASKS[task_id] for task_id in ids]

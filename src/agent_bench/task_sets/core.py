"""Core coding and design benchmark tasks."""

from .base import TaskSet

TASK_SET = TaskSet(
    id="core",
    title="Core",
    description="Bug fixing, small feature development, and system refactoring.",
    task_ids=("bug-fix", "small-feature", "system-refactor"),
)

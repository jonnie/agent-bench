"""The complete built-in benchmark catalog in its original order."""

from .base import TaskSet

TASK_SET = TaskSet(
    id="default",
    title="Default",
    description="All five built-in benchmark tasks in their original order.",
    task_ids=("bug-fix", "small-feature", "system-refactor", "security-hardening", "performance"),
)

"""Security and performance benchmark tasks."""

from .base import TaskSet

TASK_SET = TaskSet(
    id="robustness",
    title="Robustness",
    description="Security hardening and performance optimization.",
    task_ids=("security-hardening", "performance"),
)

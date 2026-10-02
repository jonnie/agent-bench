"""Named, ordered groups of benchmark tasks."""

from dataclasses import dataclass


@dataclass
class TaskSet:
    id: str
    title: str
    description: str
    task_ids: tuple[str, ...]

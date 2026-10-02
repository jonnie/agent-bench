"""Shared task model and fixture factory; candidate source remains data."""

from dataclasses import dataclass, field
from textwrap import dedent


@dataclass
class Task:
    id: str
    title: str
    category: str
    prompt: str
    files: dict[str, str]
    hidden_tests: str
    metadata: dict = field(default_factory=dict)


def _source(text: str) -> str:
    return dedent(text).lstrip("\n")


def create_task(
    task_id, title, prompt, modules, public_tests, hidden_tests, metadata=None, *, category=None
):
    """Build a task fixture, defaulting its category to its ID unless specified."""
    prompt = _source(prompt)
    files = {"solution/__init__.py": "", "README.md": f"# {title}\n\n{prompt}"}
    files.update({f"solution/{name}": _source(code) for name, code in modules.items()})
    files["tests/test_public.py"] = _source(public_tests)
    return Task(
        task_id,
        title,
        task_id if category is None else category,
        prompt,
        files,
        _source(hidden_tests),
        metadata or {},
    )

from __future__ import annotations

import asyncio
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.models.invoice import InvoiceFields

TaskStatusType = Literal[
    "uploaded", "extracting", "analyzing", "processing", "paused",
    "writing_excel", "done", "error", "cancelled",
]


def _default_model_calls() -> dict[str, dict[str, int]]:
    return {
        "gemini": {"extract": 0, "detect_multi": 0},
        "openai": {"extract": 0, "detect_multi": 0},
        "claude": {"arbitrate": 0, "extract": 0, "review_receipt": 0},
    }


class TaskState(BaseModel):
    task_id: str
    status: TaskStatusType = "uploaded"
    total_files: int = 0
    processed_files: int = 0
    current_file: str = ""
    error_message: str | None = None
    excel_ready: bool = False
    completed_results: list[InvoiceFields] = Field(default_factory=list)
    pending_files: list[str] = Field(default_factory=list)
    model_calls: dict[str, dict[str, int]] = Field(default_factory=_default_model_calls)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    finished_at: float | None = None


class TaskStore:
    """In-memory store for task states."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._versions: dict[str, int] = {}

    def create(self, task_id: str, *, status: TaskStatusType = "uploaded") -> TaskState:
        now = time.time()
        task = TaskState(task_id=task_id, status=status, created_at=now, updated_at=now)
        self._tasks[task_id] = task
        self._events[task_id] = asyncio.Event()
        self._versions[task_id] = 0
        return task

    def get(self, task_id: str) -> TaskState | None:
        return self._tasks.get(task_id)

    def get_version(self, task_id: str) -> int:
        return self._versions.get(task_id, 0)

    def task_ids(self) -> set[str]:
        return set(self._tasks)

    def notify(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.updated_at = time.time()

        ev = self._events.get(task_id)
        if ev:
            self._versions[task_id] = self._versions.get(task_id, 0) + 1
            ev.set()

    def mark_finished(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            now = time.time()
            task.finished_at = now
            task.updated_at = now

    def delete(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        self._events.pop(task_id, None)
        self._versions.pop(task_id, None)

    def cleanup_expired(self, retention_seconds: int) -> list[str]:
        cutoff = time.time() - retention_seconds
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task.finished_at is not None and task.finished_at < cutoff
        ]
        for task_id in expired:
            self.delete(task_id)
        return expired

    async def wait(
        self,
        task_id: str,
        *,
        last_seen_version: int,
        timeout: float = 0.5,
    ) -> int:
        ev = self._events.get(task_id)
        if not ev:
            return last_seen_version

        current_version = self._versions.get(task_id, last_seen_version)
        if current_version != last_seen_version:
            return current_version

        try:
            await asyncio.wait_for(asyncio.shield(ev.wait()), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            ev.clear()

        return self._versions.get(task_id, last_seen_version)


task_store = TaskStore()

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from app.config import settings
from app.models.task import task_store

TASK_DIR_PREFIX = "invoice_"


def get_task_dir(task_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{TASK_DIR_PREFIX}{task_id}"


def get_task_output_path(task_id: str) -> Path:
    return get_task_dir(task_id) / "output.xlsx"


def get_task_upload_path(task_id: str) -> Path:
    return get_task_dir(task_id) / "upload.zip"


async def delete_task_dir(task_id: str) -> None:
    await asyncio.to_thread(shutil.rmtree, get_task_dir(task_id), True)


async def cleanup_orphan_task_dirs(active_task_ids: set[str]) -> list[str]:
    cutoff = time.time() - settings.task_retention_seconds
    base_dir = Path(tempfile.gettempdir())
    removed: list[str] = []

    for path in base_dir.glob(f"{TASK_DIR_PREFIX}*"):
        if not path.is_dir():
            continue

        task_id = path.name.removeprefix(TASK_DIR_PREFIX)
        if task_id in active_task_ids:
            continue

        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue

        await asyncio.to_thread(shutil.rmtree, path, True)
        removed.append(task_id)

    return removed


async def cleanup_expired_runtime_state() -> list[str]:
    expired_task_ids = task_store.cleanup_expired(settings.task_retention_seconds)
    for task_id in expired_task_ids:
        await delete_task_dir(task_id)

    orphaned = await cleanup_orphan_task_dirs(task_store.task_ids())
    return expired_task_ids + orphaned

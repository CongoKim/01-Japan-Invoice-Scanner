import asyncio

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.models.task import task_store

router = APIRouter()


@router.get("/api/progress/{task_id}")
async def progress_stream(task_id: str):
    async def event_generator():
        last_seen_version = task_store.get_version(task_id)

        while True:
            task = task_store.get(task_id)
            if not task:
                yield {"event": "error", "data": '{"error": "任务不存在"}'}
                break

            # Exclude heavy fields from SSE payload
            payload = task.model_dump(
                exclude={
                    "completed_results",
                    "pending_files",
                    "created_at",
                    "updated_at",
                    "finished_at",
                }
            )
            import json
            yield {"event": "progress", "data": json.dumps(payload, ensure_ascii=False)}

            if task.status in ("done", "error", "cancelled"):
                break

            last_seen_version = await task_store.wait(
                task_id,
                last_seen_version=last_seen_version,
                timeout=0.5,
            )

    return EventSourceResponse(event_generator())

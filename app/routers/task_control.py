import asyncio

from fastapi import APIRouter, HTTPException

from app.models.task import task_store
from app.services.orchestrator import process_task
from app.services.task_runtime import get_task_dir, get_task_upload_path

router = APIRouter()


@router.post("/api/task/{task_id}/start")
async def start_task(task_id: str):
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "uploaded":
        raise HTTPException(400, f"当前任务状态为“{task.status}”，无法开始处理")

    zip_path = get_task_upload_path(task_id)
    if not zip_path.exists():
        raise HTTPException(404, "未找到已上传的 ZIP 文件")

    task.status = "extracting"
    task.current_file = "Extracting ZIP..."
    task_store.notify(task_id)

    asyncio.create_task(process_task(task_id, zip_path, get_task_dir(task_id)))
    return {"status": "started"}


@router.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str):
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "processing":
        raise HTTPException(400, f"当前任务状态为“{task.status}”，无法暂停")
    task.status = "paused"
    task_store.notify(task_id)
    return {"status": "paused"}


@router.post("/api/task/{task_id}/resume")
async def resume_task_endpoint(task_id: str):
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status != "paused":
        raise HTTPException(400, f"当前任务状态为“{task.status}”，无法继续")
    task.status = "processing"
    task_store.notify(task_id)
    return {"status": "resumed"}


@router.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.status in ("done", "error", "cancelled"):
        raise HTTPException(400, f"任务当前已处于“{task.status}”状态")
    task.status = "cancelled"
    task_store.notify(task_id)
    return {"status": "cancelled"}

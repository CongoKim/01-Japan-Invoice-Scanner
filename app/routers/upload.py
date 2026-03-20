import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import settings
from app.models.task import task_store
from app.services.task_runtime import delete_task_dir, get_task_dir, get_task_upload_path

router = APIRouter()
UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.post("/api/upload")
async def upload_zip(file: UploadFile = File(...)):
    task_id = uuid.uuid4().hex[:12]

    # Save uploaded ZIP to temp directory
    task_dir = get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    zip_path = get_task_upload_path(task_id)
    total_bytes = 0

    try:
        with zip_path.open("wb") as output:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_size_bytes:
                    raise HTTPException(413, "上传的 ZIP 文件过大")
                output.write(chunk)
    except Exception:
        await delete_task_dir(task_id)
        raise
    finally:
        await file.close()

    task = task_store.create(task_id)
    task.current_file = file.filename or "upload.zip"
    task_store.notify(task_id)

    return {"task_id": task_id, "status": task.status}

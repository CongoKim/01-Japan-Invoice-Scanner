from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.models.task import task_store
from app.services.task_runtime import get_task_output_path

router = APIRouter()


@router.get("/api/download/{task_id}")
async def download_excel(task_id: str):
    task = task_store.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not task.excel_ready:
        raise HTTPException(400, "Excel 尚未生成完成")

    excel_path = get_task_output_path(task_id)

    if not excel_path.exists():
        raise HTTPException(404, "未找到 Excel 文件")

    return FileResponse(
        str(excel_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"invoices_{task_id}.xlsx",
    )

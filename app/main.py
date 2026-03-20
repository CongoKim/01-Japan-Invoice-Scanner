import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import settings as app_settings
from app.routers import upload, progress, download, task_control, settings
from app.services.task_runtime import cleanup_expired_runtime_state


async def _cleanup_loop() -> None:
    while True:
        await cleanup_expired_runtime_state()
        await asyncio.sleep(app_settings.cleanup_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="Japan Invoice Scanner", lifespan=lifespan)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


app.include_router(upload.router)
app.include_router(progress.router)
app.include_router(download.router)
app.include_router(task_control.router)
app.include_router(settings.router)

# Serve static files (frontend) — must be last so API routes take priority
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

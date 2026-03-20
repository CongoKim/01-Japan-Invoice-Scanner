import io
import shutil
import unittest
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models.task import task_store
from app.services.task_runtime import get_task_dir, get_task_upload_path


class TaskStartFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self._task_ids: list[str] = []
        self.addCleanup(self._cleanup_tasks)

    def _cleanup_tasks(self):
        for task_id in self._task_ids:
            task_store.delete(task_id)
            shutil.rmtree(get_task_dir(task_id), ignore_errors=True)

    def _upload_zip(self) -> str:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("invoice.txt", "demo invoice")
        payload.seek(0)

        response = self.client.post(
            "/api/upload",
            files={"file": ("invoices.zip", payload.getvalue(), "application/zip")},
        )

        self.assertEqual(response.status_code, 200)
        task_id = response.json()["task_id"]
        self._task_ids.append(task_id)
        return task_id

    def test_upload_creates_uploaded_task_without_starting_processing(self):
        task_id = self._upload_zip()

        task = task_store.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "uploaded")
        self.assertEqual(task.current_file, "invoices.zip")
        self.assertTrue(get_task_upload_path(task_id).exists())

    def test_start_endpoint_launches_background_processing_once(self):
        task_id = self._upload_zip()
        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()
            return object()

        with patch("app.routers.task_control.asyncio.create_task", side_effect=fake_create_task):
            response = self.client.post(f"/api/task/{task_id}/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "started"})
        self.assertEqual(len(scheduled), 1)

        task = task_store.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "extracting")
        self.assertEqual(task.current_file, "Extracting ZIP...")

        second_response = self.client.post(f"/api/task/{task_id}/start")
        self.assertEqual(second_response.status_code, 400)
        self.assertIn("无法开始处理", second_response.json()["detail"])

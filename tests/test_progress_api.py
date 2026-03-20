import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.models.task import task_store


class ProgressApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.created_task_ids: list[str] = []
        self.addCleanup(self._cleanup_tasks)

    def _cleanup_tasks(self):
        for task_id in self.created_task_ids:
            task_store.delete(task_id)

    def test_missing_task_uses_dedicated_sse_event(self):
        response = self.client.get("/api/progress/missing-task")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertIn("event: task_missing", response.text)
        self.assertIn("任务不存在，可能因为服务已重启或任务已过期，请重新上传。", response.text)
        self.assertNotIn("event: error", response.text)

    def test_task_snapshot_returns_404_for_missing_task(self):
        response = self.client.get("/api/task/missing-task")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "任务不存在")

    def test_task_snapshot_returns_lightweight_status_payload(self):
        task_id = "snapshot-task"
        task = task_store.create(task_id)
        task.status = "processing"
        task.total_files = 12
        task.processed_files = 3
        task.current_file = "demo.pdf"
        task_store.notify(task_id)
        self.created_task_ids.append(task_id)

        response = self.client.get(f"/api/task/{task_id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["status"], "processing")
        self.assertEqual(payload["total_files"], 12)
        self.assertEqual(payload["processed_files"], 3)
        self.assertEqual(payload["current_file"], "demo.pdf")
        self.assertNotIn("completed_results", payload)
        self.assertNotIn("pending_files", payload)


if __name__ == "__main__":
    unittest.main()

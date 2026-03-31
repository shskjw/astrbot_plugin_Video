from __future__ import annotations

import json
from pathlib import Path

from models import TaskStatus, VideoTask


class TaskRepo:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_task_path(self, task_id: str) -> Path:
        return self.base_dir / f"{task_id}.json"

    def save(self, task: VideoTask) -> None:
        task.touch()
        path = self.get_task_path(task.task_id)
        path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, task_id: str) -> VideoTask | None:
        path = self.get_task_path(task_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return VideoTask.from_dict(data)

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_url: str = "",
        result_file: str = "",
        error_message: str = "",
        raw_response: dict | None = None,
    ) -> VideoTask | None:
        task = self.load(task_id)
        if task is None:
            return None

        task.status = status
        if result_url:
            task.result_url = result_url
        if result_file:
            task.result_file = result_file
        if error_message:
            task.error_message = error_message
        if raw_response is not None:
            task.raw_response = raw_response
        self.save(task)
        return task

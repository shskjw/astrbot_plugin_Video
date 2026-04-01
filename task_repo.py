from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiofiles

from models import TaskStatus, VideoTask


class TaskRepo:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def get_task_path(self, task_id: str) -> Path:
        return self.base_dir / f"{task_id}.json"

    async def save(self, task: VideoTask) -> None:
        task.touch()
        path = self.get_task_path(task.task_id)
        payload = json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
        async with self._lock:
            async with aiofiles.open(path, "w", encoding="utf-8") as file:
                await file.write(payload)

    async def load(self, task_id: str) -> VideoTask | None:
        path = self.get_task_path(task_id)
        if not path.exists():
            return None
        async with self._lock:
            async with aiofiles.open(path, "r", encoding="utf-8") as file:
                raw = await file.read()
        data = json.loads(raw)
        return VideoTask.from_dict(data)

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result_url: str = "",
        result_file: str = "",
        error_message: str = "",
        raw_response: dict | None = None,
    ) -> VideoTask | None:
        task = await self.load(task_id)
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
        await self.save(task)
        return task

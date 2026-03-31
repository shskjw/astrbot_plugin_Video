from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from task_service import TaskService


class WorkerManager:
    def __init__(self, task_service: TaskService):
        self.task_service = task_service
        self._tasks: set[asyncio.Task[Any]] = set()

    def submit(self, task_id: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._run(task_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        if not self._tasks:
            return

        for task in list(self._tasks):
            task.cancel()

        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error(f"关闭视频任务 worker 时出现异常: {result}")
        self._tasks.clear()

    async def _run(self, task_id: str) -> None:
        try:
            await self.task_service.process_task(task_id)
        except asyncio.CancelledError:
            logger.info(f"视频任务已取消: {task_id}")
            raise
        except Exception as exc:
            logger.error(f"视频任务执行失败 {task_id}: {exc}")

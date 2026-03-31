from __future__ import annotations

import uuid
from typing import Any

from exceptions import ProviderAPIError, TaskProcessError
from media_service import MediaService
from message_service import MessageService
from models import TaskStatus, TaskType, VideoTask
from openai_video_client import OpenAIVideoClient
from task_repo import TaskRepo


class TaskService:
    def __init__(
        self,
        task_repo: TaskRepo,
        media_service: MediaService,
        message_service: MessageService,
        client: OpenAIVideoClient,
        context: Any,
        send_video_as_url: bool = True,
    ):
        self.task_repo = task_repo
        self.media_service = media_service
        self.message_service = message_service
        self.client = client
        self.context = context
        self.send_video_as_url = send_video_as_url

    def create_task(
        self,
        *,
        task_type: TaskType,
        prompt: str,
        unified_msg_origin: str,
        images: list[str] | None = None,
    ) -> VideoTask:
        task = VideoTask(
            task_id=uuid.uuid4().hex,
            task_type=task_type,
            prompt=prompt.strip(),
            unified_msg_origin=unified_msg_origin,
            images=images or [],
            status=TaskStatus.PENDING,
        )
        self.task_repo.save(task)
        return task

    async def process_task(self, task_id: str) -> VideoTask:
        task = self.task_repo.load(task_id)
        if task is None:
            raise TaskProcessError(f"任务不存在: {task_id}")

        self.task_repo.update_status(task_id, TaskStatus.PROCESSING)
        task = self.task_repo.load(task_id)
        if task is None:
            raise TaskProcessError(f"任务状态更新后丢失: {task_id}")

        try:
            result = await self._submit_task(task)
            video_url = str(result.get("video_url", "")).strip()
            result_file = str(result.get("result_file", "")).strip()
            saved = self.task_repo.update_status(
                task_id,
                TaskStatus.SUCCESS,
                result_url=video_url,
                result_file=result_file,
                raw_response=dict(result.get("raw_response", {})),
            )
            if saved is None:
                raise TaskProcessError(f"任务保存失败: {task_id}")

            await self.message_service.send_result_notification(
                self.context,
                saved,
                prefer_url=self.send_video_as_url,
            )
            return saved
        except Exception as exc:
            message = str(exc)
            saved = self.task_repo.update_status(
                task_id,
                TaskStatus.FAILED,
                error_message=message,
                raw_response={"error": message},
            )
            if saved is not None:
                await self.message_service.send_result_notification(
                    self.context,
                    saved,
                    prefer_url=self.send_video_as_url,
                )
                return saved
            raise

    async def _submit_task(self, task: VideoTask) -> dict[str, Any]:
        if task.task_type == TaskType.TEXT:
            return await self.client.submit_text_video(task.prompt)

        if task.task_type == TaskType.FIRST_LAST:
            if len(task.images) != 2:
                raise TaskProcessError("首尾帧任务在处理时图片数量不是 2 张")
            return await self.client.submit_first_last_video(
                task.prompt,
                task.images[0],
                task.images[1],
            )

        if task.task_type == TaskType.MULTI_IMAGE:
            if len(task.images) < 2:
                raise TaskProcessError("多图任务在处理时图片数量少于 2 张")
            return await self.client.submit_multi_image_video(
                task.prompt,
                task.images,
            )

        raise ProviderAPIError(f"不支持的任务类型: {task.task_type}")

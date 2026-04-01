from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger

from models import TaskType, VideoTask


class MessageService:
    def build_submit_text(self, task: VideoTask) -> str:
        task_name = self._task_type_name(task.task_type)
        return f"{task_name}任务已提交，正在后台生成。\n任务ID: {task.task_id}"

    def build_success_text(self, task: VideoTask) -> str:
        task_name = self._task_type_name(task.task_type)
        return f"{task_name}任务生成完成。\n任务ID: {task.task_id}"

    def build_failed_text(self, task: VideoTask) -> str:
        task_name = self._task_type_name(task.task_type)
        reason = task.error_message or "未知错误"
        return f"{task_name}任务生成失败。\n任务ID: {task.task_id}\n原因: {reason}"

    async def send_text(self, event: Any, text: str) -> None:
        await event.send(event.plain_result(text))

    async def send_result_notification(
        self,
        context: Any,
        task: VideoTask,
    ) -> None:
        if not (task.result_url or task.result_file):
            await context.send_message(
                task.unified_msg_origin,
                [self.build_failed_text(task)],
            )
            return

        local_video_path = ""
        try:
            local_video_path = task.result_file or await self._download_video(task.result_url)
            if not local_video_path:
                raise RuntimeError("未能获取可发送的视频文件")

            video_component = Comp.File(file=local_video_path, name=f"{task.task_id}.mp4")
            caption = self.build_success_text(task)
            await context.send_message(
                task.unified_msg_origin,
                [video_component, Comp.Plain(caption)],
            )
            return
        except Exception as exc:
            logger.error(f"发送视频文件失败，回退链接发送: {exc}")
            if task.result_url:
                fallback_text = (
                    f"{self.build_success_text(task)}\n"
                    f"视频链接: {task.result_url}"
                )
                await context.send_message(task.unified_msg_origin, [fallback_text])
                return

            await context.send_message(
                task.unified_msg_origin,
                [self.build_failed_text(task)],
            )
        finally:
            if local_video_path and await aiofiles.os.path.exists(local_video_path):
                try:
                    await aiofiles.os.remove(local_video_path)
                except Exception as exc:
                    logger.warning(f"清理临时视频文件失败: {exc}")

    async def _download_video(self, url: str) -> str:
        url = str(url or "").strip()
        if not url:
            return ""

        suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name

        timeout = httpx.Timeout(timeout=300, connect=30)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async with aiofiles.open(temp_path, "wb") as file:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                await file.write(chunk)
            return temp_path
        except Exception:
            if await aiofiles.os.path.exists(temp_path):
                await aiofiles.os.remove(temp_path)
            raise

    def _task_type_name(self, task_type: TaskType) -> str:
        mapping = {
            TaskType.TEXT: "文生视频",
            TaskType.FIRST_LAST: "首尾帧生成视频",
            TaskType.MULTI_IMAGE: "多图生成视频",
        }
        return mapping.get(task_type, "视频")

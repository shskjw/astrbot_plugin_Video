from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

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
            logger.warning(f"任务 {task.task_id} 没有 result_url/result_file，发送失败通知")
            await context.send_message(
                task.unified_msg_origin,
                MessageChain().message(self.build_failed_text(task)),
            )
            return

        local_video_path = ""
        try:
            logger.info(
                f"任务 {task.task_id} 开始准备视频发送，"
                f"result_file={task.result_file!r}, result_url={task.result_url!r}"
            )

            local_video_path = task.result_file or await self._download_video(task.task_id, task.result_url)
            if not local_video_path:
                raise RuntimeError("未能获取可发送的视频文件")

            file_size = Path(local_video_path).stat().st_size if Path(local_video_path).exists() else -1
            logger.info(
                f"任务 {task.task_id} 视频文件准备完成，path={local_video_path}, size={file_size}"
            )

            video_component = Comp.File(file=local_video_path, name=f"{task.task_id}.mp4")
            logger.info(
                f"任务 {task.task_id} 开始发送视频文件，component={video_component.__class__.__name__}"
            )
            await context.send_message(
                task.unified_msg_origin,
                MessageChain(chain=[video_component]),
            )
            logger.info(f"任务 {task.task_id} 视频文件发送成功，开始发送完成文案")

            caption = self.build_success_text(task)
            await context.send_message(
                task.unified_msg_origin,
                MessageChain().message(caption),
            )
            logger.info(f"任务 {task.task_id} 完成文案发送成功")
            return
        except Exception as exc:
            logger.error(
                f"发送视频文件失败，回退链接发送: {exc}; "
                f"task_id={task.task_id}, result_url={task.result_url!r}, local_video_path={local_video_path!r}"
            )
            if task.result_url:
                fallback_text = (
                    f"{self.build_success_text(task)}\n"
                    f"视频链接: {task.result_url}"
                )
                try:
                    await context.send_message(
                        task.unified_msg_origin,
                        MessageChain().message(fallback_text),
                    )
                    logger.info(f"任务 {task.task_id} 已成功回退为链接发送")
                    return
                except Exception as fallback_exc:
                    logger.error(f"任务 {task.task_id} 回退链接发送也失败: {fallback_exc}")

            await context.send_message(
                task.unified_msg_origin,
                MessageChain().message(self.build_failed_text(task)),
            )
        finally:
            if local_video_path and await aiofiles.os.path.exists(local_video_path):
                try:
                    await aiofiles.os.remove(local_video_path)
                    logger.info(f"任务 {task.task_id} 临时视频文件已清理: {local_video_path}")
                except Exception as exc:
                    logger.warning(f"清理临时视频文件失败: {exc}")

    async def _download_video(self, task_id: str, url: str) -> str:
        url = str(url or "").strip()
        if not url:
            logger.warning(f"任务 {task_id} 没有可下载的视频链接")
            return ""

        suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name

        timeout = httpx.Timeout(timeout=300, connect=30)
        logger.info(f"任务 {task_id} 开始下载视频: {url}")
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    async with aiofiles.open(temp_path, "wb") as file:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                await file.write(chunk)
            logger.info(f"任务 {task_id} 视频下载完成: {temp_path}")
            return temp_path
        except Exception as exc:
            logger.error(f"任务 {task_id} 视频下载失败: {exc}")
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

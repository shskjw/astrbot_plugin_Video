from __future__ import annotations

from typing import Any

from models import TaskType, VideoTask


class MessageService:
    def build_submit_text(self, task: VideoTask) -> str:
        task_name = self._task_type_name(task.task_type)
        return f"{task_name}任务已提交，正在后台生成。\n任务ID: {task.task_id}"

    def build_success_text(self, task: VideoTask) -> str:
        task_name = self._task_type_name(task.task_type)
        lines = [f"{task_name}任务生成完成。", f"任务ID: {task.task_id}"]
        if task.result_url:
            lines.append(f"视频链接: {task.result_url}")
        return "\n".join(lines)

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
        *,
        prefer_url: bool = True,
    ) -> None:
        text = self.build_success_text(task) if task.result_url or task.result_file else self.build_failed_text(task)
        if prefer_url and task.result_url:
            await context.send_message(task.unified_msg_origin, [text])
            return

        if task.result_url:
            await context.send_message(task.unified_msg_origin, [text])
            return

        await context.send_message(task.unified_msg_origin, [self.build_failed_text(task)])

    def _task_type_name(self, task_type: TaskType) -> str:
        mapping = {
            TaskType.TEXT: "文生视频",
            TaskType.FIRST_LAST: "首尾帧生成视频",
            TaskType.MULTI_IMAGE: "多图生成视频",
        }
        return mapping.get(task_type, "视频")

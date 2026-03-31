from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from context_repo import ContextRepo
from exceptions import ConfigError, VideoPluginError
from media_service import MediaService
from message_service import MessageService
from models import PluginConfigView, TaskType
from openai_video_client import OpenAIVideoClient
from task_repo import TaskRepo
from task_service import TaskService
from usage_repo import UsageRepo
from worker import WorkerManager


@register(
    "astrbot_plugin_Video",
    "shskjw",
    "openai格式视频生成插件，支持文生视频、首尾帧生成视频、多图生成视频",
    "1.0.0",
)
class VideoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.raw_config = config
        self.plugin_config = self._load_plugin_config(config)
        self._restore_dynamic_config()
        self.prompt_presets = self.plugin_config.parse_prompt_presets()

        plugin_data_dir = self._get_plugin_data_dir()
        task_dir = plugin_data_dir / "tasks"
        usage_dir = plugin_data_dir / "usage"
        context_dir = plugin_data_dir / "contexts"

        self.media_service = MediaService(timeout=min(self.plugin_config.timeout, 60))
        self.message_service = MessageService()
        self.task_repo = TaskRepo(task_dir)
        self.context_repo = ContextRepo(
            context_dir,
            max_messages=self.plugin_config.context_max_messages,
        )
        self.cooldown_records: dict[str, datetime] = {}
        self.usage_repo = UsageRepo(
            usage_dir,
            default_user_limit=self.plugin_config.default_user_limit,
            default_group_limit=self.plugin_config.default_group_limit,
        )
        self.client = OpenAIVideoClient(self.plugin_config)
        self.task_service = TaskService(
            task_repo=self.task_repo,
            media_service=self.media_service,
            message_service=self.message_service,
            client=self.client,
            context=self.context,
            send_video_as_url=self.plugin_config.send_video_as_url,
        )
        self.worker_manager = WorkerManager(self.task_service)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def record_context_message(self, event: AstrMessageEvent):
        """记录最近消息上下文。"""
        if not self.plugin_config.enable_context:
            return

        try:
            content = (event.message_str or "").strip()
            if not content:
                return

            sender_id = self._normalize_id(event.get_sender_id())
            sender_name = getattr(event, "get_sender_name", lambda: sender_id)() or sender_id
            self.context_repo.add_message(
                event.unified_msg_origin,
                sender_id=sender_id,
                sender_name=str(sender_name),
                content=content,
                is_bot=False,
            )
        except Exception as exc:
            logger.debug(f"记录视频上下文失败: {exc}")

    @filter.llm_tool(name="generate_text_video")
    async def text_to_video_tool(self, event: AstrMessageEvent, prompt: str) -> str:
        '''文生视频工具。

        仅当用户明确要求根据文字生成视频时调用。

        Args:
            prompt(string): 视频描述文本
        '''
        try:
            prompt = (prompt or "").strip()
            if not prompt:
                return self._finalize_llm_tool_result(
                    "[TOOL_FAILED] 缺少视频描述。请自然告诉用户需要提供更明确的视频描述。"
                )

            cooldown_ok, cooldown_msg = self._check_cooldown(event)
            if not cooldown_ok:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {cooldown_msg}。请自然告诉用户稍后再来。"
                )

            allowed, block_msg = self._check_quota(event)
            if not allowed:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {block_msg}。请自然告诉用户当前无法使用。"
                )

            final_prompt, preset_name, extra_prompt = self._process_prompt_and_preset(prompt)
            final_prompt = self._merge_context_prompt(event, final_prompt)

            image_sources = await self.media_service.extract_image_sources(
                event,
                context=self.context,
            )
            self.media_service.validate_text_mode_images(len(image_sources))

            self._consume_quota(event)

            task = self.task_service.create_task(
                task_type=TaskType.TEXT,
                prompt=final_prompt,
                unified_msg_origin=event.unified_msg_origin,
            )
            submit_text = self.message_service.build_submit_text(task)
            if preset_name != "自定义":
                submit_text += f"\n预设: {preset_name}"
            if extra_prompt:
                submit_text += (
                    f"\n补充描述: {extra_prompt[:50]}"
                    f"{'...' if len(extra_prompt) > 50 else ''}"
                )

            await event.send(event.plain_result(submit_text))
            self._update_cooldown(event)
            self.worker_manager.submit(task.task_id)
            return self._finalize_llm_tool_result(
                f"[TOOL_SUCCESS] 文生视频任务已提交，任务ID为 {task.task_id}。请自然告诉用户正在处理中，避免生硬复述工具文本。"
            )
        except VideoPluginError as exc:
            return self._finalize_llm_tool_result(
                f"[TOOL_FAILED] {exc}。请自然告诉用户当前无法处理，并根据错误内容提示其调整输入。"
            )
        except Exception as exc:
            logger.error(f"处理文生视频 LLM 工具失败: {exc}")
            return self._finalize_llm_tool_result(
                "[TOOL_FAILED] 文生视频任务提交失败。请自然告诉用户这次没弄好，稍后再试。"
            )

    @filter.llm_tool(name="generate_first_last_video")
    async def first_last_to_video_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
    ) -> str:
        '''首尾帧生成视频工具。

        仅当用户明确要求使用两张图片做首尾帧过渡视频时调用。

        Args:
            prompt(string): 过渡描述文本
        '''
        try:
            prompt = (prompt or "").strip()
            if not prompt:
                return self._finalize_llm_tool_result(
                    "[TOOL_FAILED] 缺少过渡描述。请自然告诉用户需要补充过渡描述，并上传 2 张图片。"
                )

            cooldown_ok, cooldown_msg = self._check_cooldown(event)
            if not cooldown_ok:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {cooldown_msg}。请自然告诉用户稍后再来。"
                )

            allowed, block_msg = self._check_quota(event)
            if not allowed:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {block_msg}。请自然告诉用户当前无法使用。"
                )

            final_prompt, preset_name, extra_prompt = self._process_prompt_and_preset(prompt)
            final_prompt = self._merge_context_prompt(event, final_prompt)

            image_sources = await self.media_service.extract_image_sources(
                event,
                context=self.context,
            )
            self.media_service.validate_first_last_images(len(image_sources))
            data_urls = await self.media_service.convert_sources_to_data_urls(image_sources)

            self._consume_quota(event)

            task = self.task_service.create_task(
                task_type=TaskType.FIRST_LAST,
                prompt=final_prompt,
                unified_msg_origin=event.unified_msg_origin,
                images=data_urls,
            )
            submit_text = self.message_service.build_submit_text(task)
            if preset_name != "自定义":
                submit_text += f"\n预设: {preset_name}"
            if extra_prompt:
                submit_text += (
                    f"\n补充描述: {extra_prompt[:50]}"
                    f"{'...' if len(extra_prompt) > 50 else ''}"
                )

            await event.send(event.plain_result(submit_text))
            self._update_cooldown(event)
            self.worker_manager.submit(task.task_id)
            return self._finalize_llm_tool_result(
                f"[TOOL_SUCCESS] 首尾帧视频任务已提交，任务ID为 {task.task_id}。请自然告诉用户正在处理中。"
            )
        except VideoPluginError as exc:
            return self._finalize_llm_tool_result(
                f"[TOOL_FAILED] {exc}。请自然告诉用户需要重新提供符合要求的图片和描述。"
            )
        except Exception as exc:
            logger.error(f"处理首尾帧生成视频 LLM 工具失败: {exc}")
            return self._finalize_llm_tool_result(
                "[TOOL_FAILED] 首尾帧生成视频任务提交失败。请自然告诉用户这次没弄好，稍后再试。"
            )

    @filter.llm_tool(name="generate_multi_image_video")
    async def multi_image_to_video_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
    ) -> str:
        '''多图生成视频工具。

        仅当用户明确要求基于多张图片生成视频时调用。

        Args:
            prompt(string): 视频描述文本
        '''
        try:
            prompt = (prompt or "").strip()
            if not prompt:
                return self._finalize_llm_tool_result(
                    "[TOOL_FAILED] 缺少视频描述。请自然告诉用户需要补充视频描述，并上传至少 2 张图片。"
                )

            cooldown_ok, cooldown_msg = self._check_cooldown(event)
            if not cooldown_ok:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {cooldown_msg}。请自然告诉用户稍后再来。"
                )

            allowed, block_msg = self._check_quota(event)
            if not allowed:
                return self._finalize_llm_tool_result(
                    f"[TOOL_FAILED] {block_msg}。请自然告诉用户当前无法使用。"
                )

            final_prompt, preset_name, extra_prompt = self._process_prompt_and_preset(prompt)
            final_prompt = self._merge_context_prompt(event, final_prompt)

            image_sources = await self.media_service.extract_image_sources(
                event,
                context=self.context,
            )
            self.media_service.validate_multi_images(
                len(image_sources),
                self.plugin_config.max_images,
            )
            data_urls = await self.media_service.convert_sources_to_data_urls(image_sources)

            self._consume_quota(event)

            task = self.task_service.create_task(
                task_type=TaskType.MULTI_IMAGE,
                prompt=final_prompt,
                unified_msg_origin=event.unified_msg_origin,
                images=data_urls,
            )
            submit_text = self.message_service.build_submit_text(task)
            if preset_name != "自定义":
                submit_text += f"\n预设: {preset_name}"
            if extra_prompt:
                submit_text += (
                    f"\n补充描述: {extra_prompt[:50]}"
                    f"{'...' if len(extra_prompt) > 50 else ''}"
                )

            await event.send(event.plain_result(submit_text))
            self._update_cooldown(event)
            self.worker_manager.submit(task.task_id)
            return self._finalize_llm_tool_result(
                f"[TOOL_SUCCESS] 多图视频任务已提交，任务ID为 {task.task_id}。请自然告诉用户正在处理中。"
            )
        except VideoPluginError as exc:
            return self._finalize_llm_tool_result(
                f"[TOOL_FAILED] {exc}。请自然告诉用户需要重新提供符合要求的图片和描述。"
            )
        except Exception as exc:
            logger.error(f"处理多图生成视频 LLM 工具失败: {exc}")
            return self._finalize_llm_tool_result(
                "[TOOL_FAILED] 多图生成视频任务提交失败。请自然告诉用户这次没弄好，稍后再试。"
            )

    @filter.command("视频预设列表", alias={"视频预设", "video预设"})
    async def list_video_presets(self, event: AstrMessageEvent):
        """查看当前视频预设列表。"""
        if not self.prompt_presets:
            yield event.plain_result("当前没有配置任何视频预设。")
            return

        lines = ["当前视频预设列表："]
        for preset in self.prompt_presets:
            preview = preset.content[:60] + ("..." if len(preset.content) > 60 else "")
            lines.append(f"- {preset.trigger}: {preview}")
        yield event.plain_result("\n".join(lines))

    @filter.command("视频预设查看", alias={"video预设查看"})
    async def view_video_preset(self, event: AstrMessageEvent, trigger: str):
        """查看指定视频预设内容。"""
        preset = next(
            (item for item in self.prompt_presets if item.trigger == trigger.strip()),
            None,
        )
        if preset is None:
            yield event.plain_result(f"没有找到视频预设：{trigger}")
            return
        yield event.plain_result(f"[{preset.trigger}]\n{preset.content}")

    @filter.command("视频预设添加", alias={"video预设添加"})
    async def add_video_preset(self, event: AstrMessageEvent):
        """添加或更新视频预设，仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以添加视频预设。")
            return

        text = event.message_str.replace("/视频预设添加", "", 1).strip()
        text = text.replace("视频预设添加", "", 1).strip()
        if ":" not in text:
            yield event.plain_result("格式错误。用法：/视频预设添加 触发词:完整提示词")
            return

        trigger, content = text.split(":", 1)
        trigger = trigger.strip()
        content = content.strip()
        if not trigger or not content:
            yield event.plain_result("触发词和提示词都不能为空。")
            return

        prompt_list = list(self.plugin_config.prompt_list)
        prompt_list = [
            item for item in prompt_list if not item.startswith(f"{trigger}:")
        ]
        prompt_list.append(f"{trigger}:{content}")

        self.plugin_config.prompt_list = prompt_list
        self.prompt_presets = self.plugin_config.parse_prompt_presets()
        self._persist_prompt_list(prompt_list)

        yield event.plain_result(f"已添加/更新视频预设：{trigger}")

    @filter.command("视频预设删除", alias={"video预设删除"})
    async def delete_video_preset(self, event: AstrMessageEvent, trigger: str):
        """删除指定视频预设，仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以删除视频预设。")
            return

        trigger = trigger.strip()
        old_list = list(self.plugin_config.prompt_list)
        new_list = [item for item in old_list if not item.startswith(f"{trigger}:")]

        if len(new_list) == len(old_list):
            yield event.plain_result(f"没有找到视频预设：{trigger}")
            return

        self.plugin_config.prompt_list = new_list
        self.prompt_presets = self.plugin_config.parse_prompt_presets()
        self._persist_prompt_list(new_list)
        yield event.plain_result(f"已删除视频预设：{trigger}")

    @filter.command("视频查询次数", alias={"video次数"})
    async def query_video_count(self, event: AstrMessageEvent):
        """查询当前视频剩余次数。"""
        user_id = self._normalize_id(event.get_sender_id())
        group_id = self._normalize_id(event.get_group_id())

        lines = []
        if self.plugin_config.enable_user_limit:
            lines.append(f"你的剩余视频次数：{self.usage_repo.get_user_count(user_id)}")
        else:
            lines.append("用户次数限制：未启用")

        if group_id:
            if self.plugin_config.enable_group_limit:
                lines.append(
                    f"当前群剩余视频次数：{self.usage_repo.get_group_count(group_id)}"
                )
            else:
                lines.append("群组次数限制：未启用")

        yield event.plain_result("\n".join(lines))

    @filter.command("视频增加用户次数", alias={"video加用户次数"})
    async def add_video_user_count(self, event: AstrMessageEvent, user_id: str, count: int):
        """为指定用户增加视频次数，仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以增加用户视频次数。")
            return

        if count <= 0:
            yield event.plain_result("增加次数必须大于 0。")
            return

        new_count = self.usage_repo.add_user_count(self._normalize_id(user_id), count)
        yield event.plain_result(
            f"已为用户 {user_id} 增加 {count} 次，当前剩余：{new_count}"
        )

    @filter.command("视频增加群组次数", alias={"video加群次数"})
    async def add_video_group_count(self, event: AstrMessageEvent, group_id: str, count: int):
        """为指定群组增加视频次数，仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以增加群组视频次数。")
            return

        if count <= 0:
            yield event.plain_result("增加次数必须大于 0。")
            return

        new_count = self.usage_repo.add_group_count(self._normalize_id(group_id), count)
        yield event.plain_result(
            f"已为群组 {group_id} 增加 {count} 次，当前剩余：{new_count}"
        )

    @filter.command("视频签到", alias={"video签到"})
    async def video_checkin(self, event: AstrMessageEvent):
        """签到获取视频次数。"""
        if not self.plugin_config.enable_checkin:
            yield event.plain_result("当前未开启视频签到功能。")
            return

        user_id = self._normalize_id(event.get_sender_id())
        success, new_count = self.usage_repo.process_checkin(
            user_id,
            self.plugin_config.checkin_add_count,
        )
        if success:
            yield event.plain_result(
                f"签到成功，已增加 {self.plugin_config.checkin_add_count} 次视频次数，当前剩余：{new_count}"
            )
            return

        yield event.plain_result(f"今天已经签到过了，当前剩余视频次数：{new_count}")

    @filter.command("视频今日统计", alias={"video统计"})
    async def video_daily_stats(self, event: AstrMessageEvent):
        """查看今日视频使用统计，仅管理员可用。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以查看视频今日统计。")
            return

        stats = self.usage_repo.get_daily_stats()
        date = stats.get("date", "")
        total = int(stats.get("total", 0))
        users = dict(stats.get("users", {}))
        groups = dict(stats.get("groups", {}))

        lines = [f"视频今日统计：{date or '暂无数据'}", f"总调用次数：{total}"]

        if users:
            top_users = sorted(users.items(), key=lambda item: item[1], reverse=True)[:10]
            lines.append("用户排行：")
            for uid, count in top_users:
                lines.append(f"- {uid}: {count}")
        else:
            lines.append("用户排行：暂无")

        if groups:
            top_groups = sorted(groups.items(), key=lambda item: item[1], reverse=True)[:10]
            lines.append("群组排行：")
            for gid, count in top_groups:
                lines.append(f"- {gid}: {count}")
        else:
            lines.append("群组排行：暂无")

        yield event.plain_result("\n".join(lines))

    @filter.command("视频帮助", alias={"video帮助"})
    async def video_help(self, event: AstrMessageEvent):
        """查看视频插件帮助。"""
        help_text = (
            "视频插件当前支持：\n"
            "1. LLM Tool 调用：文生视频 / 首尾帧视频 / 多图视频\n"
            "2. 视频预设列表、查看、添加、删除\n"
            "3. 视频次数查询 / 视频签到\n"
            "4. 管理员增加用户次数 / 群组次数\n"
            "5. 管理员查看视频今日统计\n\n"
            "预设格式：触发词:完整提示词\n"
            "例如：电影感: cinematic camera movement, dramatic lighting"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        await self.worker_manager.shutdown()

    def _load_plugin_config(self, config: AstrBotConfig) -> PluginConfigView:
        try:
            plugin_config = PluginConfigView.from_dict(dict(config))
            plugin_config.validate()
            return plugin_config
        except ValueError as exc:
            raise ConfigError(
                f"插件配置不完整，请先在插件配置中填写 base_url、api_key、model 等必要项。详情: {exc}"
            ) from exc

    def _get_plugin_data_dir(self) -> Path:
        return get_astrbot_data_path() / "plugin_data" / self.name

    def _normalize_id(self, value: str | None) -> str:
        return str(value or "").strip()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        user_id = self._normalize_id(event.get_sender_id())
        admins = self.context.get_config().get("admins_id", [])
        return user_id in [self._normalize_id(item) for item in admins]

    def _restore_dynamic_config(self) -> None:
        try:
            config_path = self._get_plugin_data_dir() / "dynamic_config.json"
            if not config_path.exists():
                return
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return

            prompt_list = payload.get("prompt_list")
            if isinstance(prompt_list, list) and not self.plugin_config.prompt_list:
                self.plugin_config.prompt_list = [str(item) for item in prompt_list]
        except Exception as exc:
            logger.warning(f"恢复视频插件动态配置失败: {exc}")

    def _persist_prompt_list(self, prompt_list: list[str]) -> None:
        try:
            self.raw_config["prompt_list"] = prompt_list
            if hasattr(self.raw_config, "save_config") and callable(self.raw_config.save_config):
                self.raw_config.save_config()
                return
            if hasattr(self.raw_config, "save") and callable(self.raw_config.save):
                self.raw_config.save()
                return
        except Exception as exc:
            logger.warning(f"通过 AstrBotConfig 保存视频预设失败，改用本地备份: {exc}")

        try:
            config_path = self._get_plugin_data_dir() / "dynamic_config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"prompt_list": prompt_list}
            config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(f"保存视频预设本地备份失败: {exc}")

    def _check_cooldown(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if not self.plugin_config.enable_cooldown:
            return True, ""

        if self._is_admin(event):
            return True, ""

        user_id = self._normalize_id(event.get_sender_id())
        last_time = self.cooldown_records.get(user_id)
        if not last_time:
            return True, ""

        elapsed = (datetime.now() - last_time).total_seconds()
        remaining = self.plugin_config.cooldown_seconds - int(elapsed)
        if remaining > 0:
            return False, f"当前还在冷却中，请 {remaining} 秒后再试"

        return True, ""

    def _update_cooldown(self, event: AstrMessageEvent) -> None:
        if not self.plugin_config.enable_cooldown:
            return
        user_id = self._normalize_id(event.get_sender_id())
        if user_id:
            self.cooldown_records[user_id] = datetime.now()

    def _merge_context_prompt(self, event: AstrMessageEvent, prompt: str) -> str:
        if not self.plugin_config.enable_context:
            return prompt

        context_text = self.context_repo.build_context_text(
            event.unified_msg_origin,
            count=self.plugin_config.context_rounds,
        )
        if not context_text:
            return prompt

        return (
            f"{prompt}\n\n"
            f"Recent conversation context:\n{context_text}\n"
            f"Please keep the result consistent with the recent context when appropriate."
        )

    def _check_quota(self, event: AstrMessageEvent) -> tuple[bool, str]:
        user_id = self._normalize_id(event.get_sender_id())
        group_id = self._normalize_id(event.get_group_id())

        if self._is_admin(event):
            return True, ""

        if user_id and user_id in self.plugin_config.user_blacklist:
            return False, "当前用户已被限制使用视频生成功能"

        if group_id and group_id in self.plugin_config.group_blacklist:
            return False, "当前群组已被限制使用视频生成功能"

        if self.plugin_config.user_whitelist and user_id not in self.plugin_config.user_whitelist:
            return False, "当前用户不在视频功能白名单中"

        if group_id and self.plugin_config.group_whitelist and group_id not in self.plugin_config.group_whitelist:
            return False, "当前群组不在视频功能白名单中"

        if self.plugin_config.enable_user_limit:
            user_count = self.usage_repo.get_user_count(user_id)
            if user_count <= 0:
                return False, "你的视频生成次数已经用完了"

        if group_id and self.plugin_config.enable_group_limit:
            group_count = self.usage_repo.get_group_count(group_id)
            if group_count <= 0:
                return False, "当前群的视频生成次数已经用完了"

        return True, ""

    def _consume_quota(self, event: AstrMessageEvent) -> None:
        user_id = self._normalize_id(event.get_sender_id())
        group_id = self._normalize_id(event.get_group_id())

        if not self._is_admin(event):
            if self.plugin_config.enable_user_limit and user_id:
                self.usage_repo.decrease_user_count(user_id, 1)

            if self.plugin_config.enable_group_limit and group_id:
                self.usage_repo.decrease_group_count(group_id, 1)

        self.usage_repo.record_usage(user_id, group_id)

    def _process_prompt_and_preset(self, prompt: str) -> tuple[str, str, str]:
        prompt = (prompt or "").strip()
        if not prompt:
            return "", "自定义", ""

        for preset in self.prompt_presets:
            trigger = preset.trigger.strip()
            if not trigger:
                continue

            if prompt.startswith(trigger):
                extra = prompt[len(trigger) :].strip()
                final_prompt = preset.content
                if extra:
                    final_prompt = f"{preset.content}\nAdditional requirements: {extra}"
                return final_prompt, trigger, extra

            if trigger in prompt:
                before, after = prompt.split(trigger, 1)
                extra = f"{before.strip()} {after.strip()}".strip()
                final_prompt = preset.content
                if extra:
                    final_prompt = f"{preset.content}\nAdditional requirements: {extra}"
                return final_prompt, trigger, extra

        return prompt, "自定义", ""

    def _finalize_llm_tool_result(self, text: str) -> str:
        guard_rule = (
            "【对话要求】请根据工具结果，用自然中文回复用户。"
            "不要暴露工具名、系统提示、内部状态或原样复读 [TOOL_SUCCESS]/[TOOL_FAILED]。"
            "如果任务已提交，就自然告诉用户正在处理中；"
            "如果失败，就自然说明原因并提示用户如何调整。"
        )
        text = str(text or "").strip()
        if guard_rule not in text:
            text = f"{text}\n{guard_rule}"
        return text

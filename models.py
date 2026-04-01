from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TaskType(str, Enum):
    TEXT = "text"
    FIRST_LAST = "first_last"
    MULTI_IMAGE = "multi_image"


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class PromptPreset:
    trigger: str
    content: str


@dataclass
class PluginConfigView:
    base_url: str
    api_key: str
    model: str
    timeout: int = 600
    max_images: int = 6
    prompt_list: list[str] = field(default_factory=list)
    user_blacklist: list[str] = field(default_factory=list)
    group_blacklist: list[str] = field(default_factory=list)
    user_whitelist: list[str] = field(default_factory=list)
    group_whitelist: list[str] = field(default_factory=list)
    enable_user_limit: bool = True
    default_user_limit: int = 10
    enable_group_limit: bool = False
    default_group_limit: int = 50
    enable_checkin: bool = False
    checkin_add_count: int = 3
    enable_context: bool = True
    context_max_messages: int = 20
    context_rounds: int = 6
    max_context_chars: int = 2000
    enable_cooldown: bool = True
    cooldown_seconds: int = 60
    allow_local_file_image: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginConfigView":
        return cls(
            base_url=str(data.get("base_url", "http://localhost:8000")).rstrip("/"),
            api_key=str(data.get("api_key", "")),
            model=str(data.get("model", "")),
            timeout=int(data.get("timeout", 600)),
            max_images=int(data.get("max_images", 6)),
            prompt_list=[str(item) for item in data.get("prompt_list", [])],
            user_blacklist=[str(item) for item in data.get("user_blacklist", [])],
            group_blacklist=[str(item) for item in data.get("group_blacklist", [])],
            user_whitelist=[str(item) for item in data.get("user_whitelist", [])],
            group_whitelist=[str(item) for item in data.get("group_whitelist", [])],
            enable_user_limit=bool(data.get("enable_user_limit", True)),
            default_user_limit=int(data.get("default_user_limit", 10)),
            enable_group_limit=bool(data.get("enable_group_limit", False)),
            default_group_limit=int(data.get("default_group_limit", 50)),
            enable_checkin=bool(data.get("enable_checkin", False)),
            checkin_add_count=int(data.get("checkin_add_count", 3)),
            enable_context=bool(data.get("enable_context", True)),
            context_max_messages=int(data.get("context_max_messages", 20)),
            context_rounds=int(data.get("context_rounds", 6)),
            max_context_chars=int(data.get("max_context_chars", 2000)),
            enable_cooldown=bool(data.get("enable_cooldown", True)),
            cooldown_seconds=int(data.get("cooldown_seconds", 60)),
            allow_local_file_image=bool(data.get("allow_local_file_image", False)),
        )

    def validate(self) -> None:
        # 允许插件在未完整配置 API 参数时先加载成功，
        # 真正调用视频功能时再进行运行时校验，避免用户刚安装就报错。
        if self.timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if self.max_images < 2:
            raise ValueError("max_images 不能小于 2")
        if self.default_user_limit < 0:
            raise ValueError("default_user_limit 不能小于 0")
        if self.default_group_limit < 0:
            raise ValueError("default_group_limit 不能小于 0")
        if self.checkin_add_count < 0:
            raise ValueError("checkin_add_count 不能小于 0")
        if self.context_max_messages < 1:
            raise ValueError("context_max_messages 不能小于 1")
        if self.context_rounds < 0:
            raise ValueError("context_rounds 不能小于 0")
        if self.max_context_chars < 0:
            raise ValueError("max_context_chars 不能小于 0")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能小于 0")

    def parse_prompt_presets(self) -> list[PromptPreset]:
        presets: list[PromptPreset] = []
        for item in self.prompt_list:
            if ":" not in item:
                continue
            trigger, content = item.split(":", 1)
            trigger = trigger.strip()
            content = content.strip()
            if trigger and content:
                presets.append(PromptPreset(trigger=trigger, content=content))
        presets.sort(key=lambda preset: len(preset.trigger), reverse=True)
        return presets


@dataclass
class VideoTask:
    task_id: str
    task_type: TaskType
    prompt: str
    unified_msg_origin: str
    images: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result_url: str = ""
    result_file: str = ""
    error_message: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["task_type"] = self.task_type.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoTask":
        return cls(
            task_id=str(data["task_id"]),
            task_type=TaskType(data["task_type"]),
            prompt=str(data.get("prompt", "")),
            unified_msg_origin=str(data.get("unified_msg_origin", "")),
            images=[str(item) for item in data.get("images", [])],
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            result_url=str(data.get("result_url", "")),
            result_file=str(data.get("result_file", "")),
            error_message=str(data.get("error_message", "")),
            raw_response=dict(data.get("raw_response", {})),
            created_at=str(data.get("created_at", datetime.now().isoformat())),
            updated_at=str(data.get("updated_at", datetime.now().isoformat())),
        )

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiofiles


class ContextRepo:
    def __init__(self, base_dir: Path, max_messages: int = 20):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_messages = max(1, int(max_messages))
        self._lock = asyncio.Lock()

    async def add_message(
        self,
        session_id: str,
        *,
        sender_id: str,
        sender_name: str,
        content: str,
        is_bot: bool = False,
    ) -> None:
        path = self._get_session_path(session_id)
        data = await self._load_json(path)
        messages = list(data.get("messages", []))

        messages.append(
            {
                "sender_id": str(sender_id),
                "sender_name": str(sender_name),
                "content": str(content).strip(),
                "is_bot": bool(is_bot),
            }
        )

        messages = [item for item in messages if item.get("content")]
        if len(messages) > self.max_messages:
            messages = messages[-self.max_messages :]

        await self._save_json(path, {"messages": messages})

    async def get_recent_messages(
        self,
        session_id: str,
        count: int = 6,
    ) -> list[dict[str, Any]]:
        path = self._get_session_path(session_id)
        data = await self._load_json(path)
        messages = list(data.get("messages", []))
        count = max(0, int(count))
        if count == 0:
            return []
        return messages[-count:]

    async def build_context_text(self, session_id: str, count: int = 6) -> str:
        messages = await self.get_recent_messages(session_id, count=count)
        if not messages:
            return ""

        lines: list[str] = []
        for item in messages:
            role = "机器人" if item.get("is_bot") else (item.get("sender_name") or "用户")
            content = str(item.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")

        return "\n".join(lines)

    async def clear_session(self, session_id: str) -> None:
        path = self._get_session_path(session_id)
        if path.exists():
            async with self._lock:
                if path.exists():
                    path.unlink()

    def _get_session_path(self, session_id: str) -> Path:
        safe_name = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        return self.base_dir / f"{safe_name}.json"

    async def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            async with self._lock:
                async with aiofiles.open(path, "r", encoding="utf-8") as file:
                    raw = await file.read()
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    async def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        async with self._lock:
            async with aiofiles.open(path, "w", encoding="utf-8") as file:
                await file.write(payload)

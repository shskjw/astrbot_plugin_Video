from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ContextRepo:
    def __init__(self, base_dir: Path, max_messages: int = 20):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_messages = max(1, int(max_messages))

    def add_message(
        self,
        session_id: str,
        *,
        sender_id: str,
        sender_name: str,
        content: str,
        is_bot: bool = False,
    ) -> None:
        path = self._get_session_path(session_id)
        data = self._load_json(path)
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

        self._save_json(path, {"messages": messages})

    def get_recent_messages(self, session_id: str, count: int = 6) -> list[dict[str, Any]]:
        path = self._get_session_path(session_id)
        data = self._load_json(path)
        messages = list(data.get("messages", []))
        count = max(0, int(count))
        if count == 0:
            return []
        return messages[-count:]

    def build_context_text(self, session_id: str, count: int = 6) -> str:
        messages = self.get_recent_messages(session_id, count=count)
        if not messages:
            return ""

        lines: list[str] = []
        for item in messages:
            role = "机器人" if item.get("is_bot") else (item.get("sender_name") or "用户")
            content = str(item.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")

        return "\n".join(lines)

    def clear_session(self, session_id: str) -> None:
        path = self._get_session_path(session_id)
        if path.exists():
            path.unlink()

    def _get_session_path(self, session_id: str) -> Path:
        safe_name = session_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        return self.base_dir / f"{safe_name}.json"

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

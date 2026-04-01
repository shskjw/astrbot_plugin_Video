from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import aiofiles


class UsageRepo:
    def __init__(
        self,
        base_dir: Path,
        *,
        default_user_limit: int = 10,
        default_group_limit: int = 50,
    ):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.default_user_limit = max(0, int(default_user_limit))
        self.default_group_limit = max(0, int(default_group_limit))
        self.user_file = self.base_dir / "user_usage.json"
        self.group_file = self.base_dir / "group_usage.json"
        self.checkin_file = self.base_dir / "checkin.json"
        self.daily_stats_file = self.base_dir / "daily_stats.json"
        self._lock = asyncio.Lock()

    async def get_user_count(self, user_id: str) -> int:
        data = await self._load_json(self.user_file)
        if user_id not in data:
            data[user_id] = self.default_user_limit
            await self._save_json(self.user_file, data)
        return max(0, int(data.get(user_id, self.default_user_limit)))

    async def get_group_count(self, group_id: str) -> int:
        data = await self._load_json(self.group_file)
        if group_id not in data:
            data[group_id] = self.default_group_limit
            await self._save_json(self.group_file, data)
        return max(0, int(data.get(group_id, self.default_group_limit)))

    async def decrease_user_count(self, user_id: str, count: int = 1) -> int:
        data = await self._load_json(self.user_file)
        current = await self.get_user_count(user_id)
        data[user_id] = max(0, current - max(0, int(count)))
        await self._save_json(self.user_file, data)
        return int(data[user_id])

    async def decrease_group_count(self, group_id: str, count: int = 1) -> int:
        data = await self._load_json(self.group_file)
        current = await self.get_group_count(group_id)
        data[group_id] = max(0, current - max(0, int(count)))
        await self._save_json(self.group_file, data)
        return int(data[group_id])

    async def add_user_count(self, user_id: str, count: int) -> int:
        data = await self._load_json(self.user_file)
        current = await self.get_user_count(user_id)
        data[user_id] = current + max(0, int(count))
        await self._save_json(self.user_file, data)
        return int(data[user_id])

    async def add_group_count(self, group_id: str, count: int) -> int:
        data = await self._load_json(self.group_file)
        current = await self.get_group_count(group_id)
        data[group_id] = current + max(0, int(count))
        await self._save_json(self.group_file, data)
        return int(data[group_id])

    async def process_checkin(self, user_id: str, add_count: int) -> tuple[bool, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self._load_json(self.checkin_file)
        last_date = str(data.get(user_id, ""))
        if last_date == today:
            return False, await self.get_user_count(user_id)

        data[user_id] = today
        await self._save_json(self.checkin_file, data)
        new_count = await self.add_user_count(user_id, max(0, int(add_count)))
        return True, new_count

    async def record_usage(self, user_id: str, group_id: str = "") -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        stats = await self._load_daily_stats()

        if stats.get("date") != today:
            stats = {
                "date": today,
                "total": 0,
                "users": {},
                "groups": {},
            }

        stats["total"] = int(stats.get("total", 0)) + 1

        users = dict(stats.get("users", {}))
        if user_id:
            users[user_id] = int(users.get(user_id, 0)) + 1
        stats["users"] = users

        groups = dict(stats.get("groups", {}))
        if group_id:
            groups[group_id] = int(groups.get(group_id, 0)) + 1
        stats["groups"] = groups

        await self._save_json(self.daily_stats_file, stats)

    async def get_daily_stats(self) -> dict:
        return await self._load_daily_stats()

    async def _load_daily_stats(self) -> dict:
        data = await self._load_json(self.daily_stats_file)
        if not data:
            return {"date": "", "total": 0, "users": {}, "groups": {}}
        data.setdefault("date", "")
        data.setdefault("total", 0)
        data.setdefault("users", {})
        data.setdefault("groups", {})
        return data

    async def _load_json(self, path: Path) -> dict:
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

    async def _save_json(self, path: Path, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        async with self._lock:
            async with aiofiles.open(path, "w", encoding="utf-8") as file:
                await file.write(payload)

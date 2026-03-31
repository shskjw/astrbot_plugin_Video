from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any
import inspect
import re

import httpx

from exceptions import ImageCountError


class MediaService:
    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    async def extract_image_sources(self, event: Any, context: Any = None) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        message_chain = getattr(message_obj, "message", None) if message_obj else None

        results: list[str] = []
        seen_sources: set[str] = set()

        def add_source(source: str) -> None:
            source = str(source or "").strip()
            if not source:
                return
            if source in seen_sources:
                return
            seen_sources.add(source)
            results.append(source)

        if message_chain:
            for component in message_chain:
                if self._is_reply_component(component):
                    reply_sources = await self._extract_reply_image_sources(
                        component,
                        context=context,
                    )
                    for source in reply_sources:
                        add_source(source)
                    continue

                image_source = self._extract_image_source_from_component(component)
                if image_source:
                    add_source(image_source)

                for source in self._extract_image_sources_from_text_component(component):
                    add_source(source)

        for source in self._extract_image_sources_from_event_text(event):
            add_source(source)

        return results

    async def convert_sources_to_data_urls(self, sources: list[str]) -> list[str]:
        results: list[str] = []
        for source in sources:
            results.append(await self._source_to_data_url(source))
        return results

    def validate_text_mode_images(self, image_count: int) -> None:
        if image_count > 0:
            raise ImageCountError("文生视频不需要图片，请改用“首尾帧生成视频”或“多图生成视频”指令。")

    def validate_first_last_images(self, image_count: int) -> None:
        if image_count != 2:
            raise ImageCountError(
                f"首尾帧生成视频需要正好 2 张图片，当前检测到 {image_count} 张，请调整后重试。"
            )

    def validate_multi_images(self, image_count: int, max_images: int) -> None:
        if image_count < 2:
            raise ImageCountError(
                f"多图生成视频至少需要 2 张图片，当前检测到 {image_count} 张。"
            )
        if image_count > max_images:
            raise ImageCountError(
                f"多图生成视频最多支持 {max_images} 张图片，当前检测到 {image_count} 张。"
            )

    def _extract_image_source_from_component(self, component: Any) -> str:
        component_type = self._safe_lower(getattr(component, "type", ""))
        class_name = component.__class__.__name__.lower()

        is_image_like = component_type == "image" or "image" in class_name
        if isinstance(component, dict):
            dict_type = self._safe_lower(component.get("type", ""))
            dict_name = self._safe_lower(component.get("__class__", ""))
            is_image_like = is_image_like or dict_type == "image" or "image" in dict_name

        if not is_image_like:
            return ""

        for attr_name in ["url", "file", "path", "image"]:
            value = getattr(component, attr_name, None)
            if self._is_probably_valid_source(value):
                return str(value).strip()

        for attr_name in ["data", "raw"]:
            value = getattr(component, attr_name, None)
            if self._is_probably_valid_source(value):
                return str(value).strip()
            if isinstance(value, dict):
                nested_source = self._extract_image_source_from_mapping(value)
                if nested_source:
                    return nested_source

        if isinstance(component, dict):
            nested_source = self._extract_image_source_from_mapping(component)
            if nested_source:
                return nested_source

        return ""

    async def _source_to_data_url(self, source: str) -> str:
        source = source.strip()
        if source.startswith("data:image/"):
            return source

        if source.startswith(("http://", "https://")):
            return await self._url_to_data_url(source)

        path = Path(source)
        if path.exists() and path.is_file():
            return self._file_to_data_url(path)

        if self._looks_like_base64(source):
            return f"data:image/jpeg;base64,{source}"

        raise ImageCountError(f"无法识别图片来源：{source}")

    async def _url_to_data_url(self, url: str) -> str:
        timeout = httpx.Timeout(timeout=self.timeout, connect=min(self.timeout, 20))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImageCountError(f"下载图片失败：{exc}") from exc

        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        encoded = base64.b64encode(response.content).decode("utf-8")
        return f"data:{content_type};base64,{encoded}"

    def _file_to_data_url(self, path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    async def _extract_reply_image_sources(
        self,
        reply_component: Any,
        context: Any = None,
    ) -> list[str]:
        results: list[str] = []
        seen_sources: set[str] = set()

        def add_source(source: str) -> None:
            source = str(source or "").strip()
            if not source:
                return
            if source in seen_sources:
                return
            seen_sources.add(source)
            results.append(source)

        reply_chain = getattr(reply_component, "chain", None)
        found_in_chain = False
        if reply_chain:
            for item in list(reply_chain):
                source = self._extract_image_source_from_component(item)
                if source:
                    found_in_chain = True
                    add_source(source)
                for text_source in self._extract_image_sources_from_text_component(item):
                    found_in_chain = True
                    add_source(text_source)

        reply_id = getattr(reply_component, "id", None)
        if (not found_in_chain) and context and reply_id:
            components = await self._fetch_reply_components(context, reply_id)
            for item in components:
                source = self._extract_image_source_from_component(item)
                if source:
                    add_source(source)
                for text_source in self._extract_image_sources_from_text_component(item):
                    add_source(text_source)

        return results

    async def _fetch_reply_components(self, context: Any, reply_id: Any) -> list[Any]:
        bot = await self._resolve_bot_for_context(context)
        if not bot or not reply_id:
            return []

        for method_name in ("get_message", "fetch_message", "get_msg", "get_reply_message"):
            if not hasattr(bot, method_name):
                continue
            try:
                method = getattr(bot, method_name)
                result = method(reply_id)
                if inspect.isawaitable(result):
                    result = await result

                if not result:
                    continue
                if hasattr(result, "message_obj") and hasattr(result.message_obj, "message"):
                    return list(result.message_obj.message)
                if hasattr(result, "message"):
                    return list(result.message)
                if isinstance(result, list):
                    return result
            except Exception:
                continue

        return []

    async def _resolve_bot_for_context(self, context: Any) -> Any:
        candidate_attrs = [
            "get_bot",
            "get_robot",
            "get_adapter",
            "get_client",
            "bot",
            "robot",
            "adapter",
            "client",
        ]
        for attr_name in candidate_attrs:
            if not hasattr(context, attr_name):
                continue
            try:
                target = getattr(context, attr_name)
                value = target() if callable(target) else target
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value
            except Exception:
                continue
        return None

    def _extract_image_sources_from_event_text(self, event: Any) -> list[str]:
        text = getattr(event, "message_str", "") or ""
        return self._extract_image_sources_from_text(text)

    def _extract_image_sources_from_text_component(self, component: Any) -> list[str]:
        texts: list[str] = []

        if isinstance(component, dict):
            for key in ["text", "content"]:
                value = component.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
        else:
            for attr_name in ["text", "content"]:
                value = getattr(component, attr_name, None)
                if isinstance(value, str) and value.strip():
                    texts.append(value)

        results: list[str] = []
        seen: set[str] = set()
        for text in texts:
            for source in self._extract_image_sources_from_text(text):
                if source not in seen:
                    seen.add(source)
                    results.append(source)
        return results

    def _extract_image_sources_from_text(self, text: str) -> list[str]:
        if not text:
            return []

        results: list[str] = []
        seen: set[str] = set()

        http_matches = re.findall(
            r"https?://[^\s<>\]\)\"']+",
            text,
            flags=re.IGNORECASE,
        )
        for match in http_matches:
            lowered = match.lower()
            if any(
                lowered.endswith(ext)
                for ext in [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]
            ) or "image" in lowered:
                if match not in seen:
                    seen.add(match)
                    results.append(match)

        data_matches = re.findall(
            r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\n\r]+",
            text,
            flags=re.IGNORECASE,
        )
        for match in data_matches:
            if match not in seen:
                seen.add(match)
                results.append(match)

        base64_matches = re.findall(
            r"base64://[A-Za-z0-9+/=\n\r]+",
            text,
            flags=re.IGNORECASE,
        )
        for match in base64_matches:
            if match not in seen:
                seen.add(match)
                results.append(match)

        return results

    def _extract_image_source_from_mapping(self, data: dict[str, Any]) -> str:
        for key in ["url", "file", "path", "image"]:
            value = data.get(key)
            if self._is_probably_valid_source(value):
                return str(value).strip()

        for key in ["data", "raw", "image_url"]:
            value = data.get(key)
            if self._is_probably_valid_source(value):
                return str(value).strip()
            if isinstance(value, dict):
                nested = self._extract_image_source_from_mapping(value)
                if nested:
                    return nested

        return ""

    def _is_reply_component(self, component: Any) -> bool:
        class_name = component.__class__.__name__.lower()
        if class_name == "reply" or self._safe_lower(getattr(component, "type", "")) == "reply":
            return True
        if isinstance(component, dict):
            return self._safe_lower(component.get("type", "")) == "reply"
        return False

    def _is_probably_valid_source(self, source: Any) -> bool:
        if source is None:
            return False
        try:
            value = str(source).strip()
        except Exception:
            return False
        if not value:
            return False
        if value.startswith(("http://", "https://", "data:image/", "base64://")):
            return True
        if len(value) < 512:
            try:
                return Path(value).is_file()
            except Exception:
                return False
        return self._looks_like_base64(value)

    def _looks_like_base64(self, value: str) -> bool:
        if len(value) < 64:
            return False
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r"
        )
        return all(char in allowed for char in value)

    def _safe_lower(self, value: Any) -> str:
        return str(value).lower() if value is not None else ""

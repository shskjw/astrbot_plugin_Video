from __future__ import annotations

import base64
import inspect
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger

from exceptions import ImageCountError


class MediaService:
    def __init__(
        self,
        timeout: int = 60,
        max_image_bytes: int = 5 * 1024 * 1024,
        allow_local_file_image: bool = False,
    ):
        self.timeout = timeout
        self.max_image_bytes = max(1024, int(max_image_bytes))
        self.allow_local_file_image = bool(allow_local_file_image)

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

        normalized_chain = self._flatten_components(message_chain)
        if normalized_chain:
            logger.debug(f"提取图片来源：当前消息链组件数 {len(normalized_chain)}")
            for component in normalized_chain:
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
        if isinstance(component, (list, tuple)):
            return ""

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
            return self._validate_data_url(source)

        if source.startswith(("http://", "https://")):
            return await self._url_to_data_url(source)

        if source.startswith("base64://"):
            return self._base64_to_data_url(source[len("base64://") :].strip())

        if self._looks_like_base64(source):
            return self._base64_to_data_url(source)

        path = Path(source)
        if path.exists() and path.is_file():
            if not self.allow_local_file_image:
                raise ImageCountError("当前环境禁止读取本地文件图片，请改用消息图片或公网图片链接。")
            return self._file_to_data_url(path)

        raise ImageCountError(f"无法识别图片来源：{source}")

    async def _url_to_data_url(self, url: str) -> str:
        timeout = httpx.Timeout(timeout=self.timeout, connect=min(self.timeout, 20))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length", "").strip()
                    if content_length.isdigit() and int(content_length) > self.max_image_bytes:
                        raise ImageCountError(
                            f"图片过大，已超过安全限制 {self.max_image_bytes} 字节"
                        )

                    content = await self._read_limited_bytes(response, self.max_image_bytes)
                    content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        except ImageCountError:
            raise
        except httpx.HTTPError as exc:
            raise ImageCountError(f"下载图片失败：{exc}") from exc

        encoded = base64.b64encode(content).decode("utf-8")
        return f"data:{content_type};base64,{encoded}"

    def _file_to_data_url(self, path: Path) -> str:
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise ImageCountError(f"读取本地图片失败：{exc}") from exc

        if file_size > self.max_image_bytes:
            raise ImageCountError(f"本地图片过大，已超过安全限制 {self.max_image_bytes} 字节")

        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    def _validate_data_url(self, source: str) -> str:
        prefix, sep, encoded = source.partition(",")
        if not sep or not encoded:
            raise ImageCountError("图片 data URL 格式无效")

        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ImageCountError(f"图片 data URL 解析失败：{exc}") from exc

        if len(raw) > self.max_image_bytes:
            raise ImageCountError(f"图片过大，已超过安全限制 {self.max_image_bytes} 字节")

        return source

    def _base64_to_data_url(self, encoded: str) -> str:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ImageCountError(f"图片 base64 数据无效：{exc}") from exc

        if len(raw) > self.max_image_bytes:
            raise ImageCountError(f"图片过大，已超过安全限制 {self.max_image_bytes} 字节")

        return f"data:image/jpeg;base64,{encoded}"

    async def _read_limited_bytes(
        self,
        response: httpx.Response,
        limit: int,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0

        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise ImageCountError(f"图片过大，已超过安全限制 {limit} 字节")
            chunks.append(chunk)

        return b"".join(chunks)

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

        reply_chain = self._extract_reply_chain(reply_component)
        logger.debug(
            f"提取回复图片：reply_component_type={type(reply_component).__name__}, "
            f"reply_chain_len={len(reply_chain)}"
        )

        found_in_chain = False
        if reply_chain:
            for item in reply_chain:
                source = self._extract_image_source_from_component(item)
                if source:
                    found_in_chain = True
                    add_source(source)
                for text_source in self._extract_image_sources_from_text_component(item):
                    found_in_chain = True
                    add_source(text_source)

        reply_id = self._extract_reply_id(reply_component)
        if (not found_in_chain) and context and reply_id:
            components = await self._fetch_reply_components(context, reply_id)
            logger.debug(
                f"提取回复图片：reply_id={reply_id}, fetched_components={len(components)}"
            )
            for item in components:
                source = self._extract_image_source_from_component(item)
                if source:
                    add_source(source)
                for text_source in self._extract_image_sources_from_text_component(item):
                    add_source(text_source)

        return results

    def _extract_reply_chain(self, reply_component: Any) -> list[Any]:
        if isinstance(reply_component, dict):
            raw_chain = reply_component.get("chain")
        else:
            raw_chain = getattr(reply_component, "chain", None)
        return self._flatten_components(raw_chain)

    def _extract_reply_id(self, reply_component: Any) -> Any:
        if isinstance(reply_component, dict):
            return reply_component.get("id")
        return getattr(reply_component, "id", None)

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
                    return self._flatten_components(result.message_obj.message)
                if hasattr(result, "message"):
                    return self._flatten_components(result.message)
                if isinstance(result, list):
                    return self._flatten_components(result)
            except Exception as exc:
                logger.debug(f"获取回复消息失败 {method_name}: {exc}")
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
        if isinstance(component, (list, tuple)):
            results: list[str] = []
            seen: set[str] = set()
            for item in self._flatten_components(component):
                for source in self._extract_image_sources_from_text_component(item):
                    if source not in seen:
                        seen.add(source)
                        results.append(source)
            return results

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
        if isinstance(component, (list, tuple)):
            return False

        class_name = component.__class__.__name__.lower()
        if class_name == "reply" or self._safe_lower(getattr(component, "type", "")) == "reply":
            return True
        if isinstance(component, dict):
            return self._safe_lower(component.get("type", "")) == "reply"
        return False

    def _flatten_components(self, components: Any) -> list[Any]:
        if components is None:
            return []
        if not isinstance(components, (list, tuple)):
            return [components]

        flattened: list[Any] = []
        for item in components:
            if isinstance(item, (list, tuple)):
                flattened.extend(self._flatten_components(item))
            elif item is not None:
                flattened.append(item)
        return flattened

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

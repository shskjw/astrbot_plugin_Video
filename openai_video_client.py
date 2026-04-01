from __future__ import annotations

import json
import re
from typing import Any

import httpx

from exceptions import ProviderAPIError
from models import PluginConfigView

DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


class OpenAIVideoClient:
    def __init__(self, config: PluginConfigView):
        self.config = config

    async def submit_text_video(self, prompt: str) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        return await self._post_chat_completions(payload)

    async def submit_first_last_video(
        self,
        prompt: str,
        first_image: str,
        last_image: str,
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": first_image}},
                        {"type": "image_url", "image_url": {"url": last_image}},
                    ],
                }
            ],
            "stream": True,
        }
        return await self._post_chat_completions(payload)

    async def submit_multi_image_video(
        self,
        prompt: str,
        images: list[str],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": image}})

        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "stream": True,
        }
        return await self._post_chat_completions(payload)

    async def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(
            timeout=self.config.timeout,
            connect=min(self.config.timeout, 30),
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    raw_bytes = await self._read_limited_response(response)
                    raw_text = raw_bytes.decode("utf-8", errors="ignore")
        except httpx.HTTPStatusError as exc:
            body = ""
            if exc.response is not None:
                try:
                    body = exc.response.text
                except Exception:
                    body = ""
            raise ProviderAPIError(
                f"视频接口请求失败，HTTP {exc.response.status_code if exc.response else 'unknown'}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderAPIError(f"视频接口请求异常: {exc}") from exc

        parsed = self._parse_response_text(raw_text)
        parsed["raw_text"] = raw_text
        return parsed

    async def _read_limited_response(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        max_bytes = DEFAULT_MAX_RESPONSE_BYTES

        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ProviderAPIError(
                    f"视频接口响应过大，已超过安全限制 {max_bytes} 字节"
                )
            chunks.append(chunk)

        return b"".join(chunks)

    def _parse_response_text(self, raw_text: str) -> dict[str, Any]:
        raw_text = raw_text.strip()
        if not raw_text:
            raise ProviderAPIError("视频接口返回为空")

        if raw_text.startswith("{") or raw_text.startswith("["):
            try:
                data = json.loads(raw_text)
                return self._extract_result_from_data(data)
            except json.JSONDecodeError:
                pass

        sse_objects = self._parse_sse_lines(raw_text)
        if sse_objects:
            merged_text = "\n".join(
                item
                for item in (self._extract_text_fragment(obj) for obj in sse_objects)
                if item
            )
            extracted = self._extract_video_info_from_objects(sse_objects)
            if merged_text and "text" not in extracted:
                extracted["text"] = merged_text
            return extracted

        url = self._extract_url(raw_text)
        if url:
            return {
                "video_url": url,
                "text": raw_text,
                "raw_response": {"raw_text": raw_text},
            }

        return {
            "text": raw_text,
            "raw_response": {"raw_text": raw_text},
        }

    def _parse_sse_lines(self, raw_text: str) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                objects.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return objects

    def _extract_result_from_data(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            direct_url = self._find_video_url_in_mapping(data)
            if direct_url:
                return {
                    "video_url": direct_url,
                    "raw_response": data,
                    "text": self._extract_text_from_mapping(data),
                }

            choices = data.get("choices")
            if isinstance(choices, list):
                extracted = self._extract_video_info_from_objects(choices)
                extracted["raw_response"] = data
                return extracted

            output = data.get("output")
            if isinstance(output, dict):
                direct_url = self._find_video_url_in_mapping(output)
                if direct_url:
                    return {
                        "video_url": direct_url,
                        "raw_response": data,
                        "text": self._extract_text_from_mapping(output),
                    }

            inner_data = data.get("data")
            if isinstance(inner_data, dict):
                direct_url = self._find_video_url_in_mapping(inner_data)
                if direct_url:
                    return {
                        "video_url": direct_url,
                        "raw_response": data,
                        "text": self._extract_text_from_mapping(inner_data),
                    }

            text = self._extract_text_from_mapping(data)
            return {"text": text, "raw_response": data}

        if isinstance(data, list):
            extracted = self._extract_video_info_from_objects(data)
            extracted["raw_response"] = {"items": data}
            return extracted

        return {"text": str(data), "raw_response": {"value": data}}

    def _extract_video_info_from_objects(self, objects: list[Any]) -> dict[str, Any]:
        text_parts: list[str] = []
        for item in objects:
            if not isinstance(item, dict):
                continue

            direct_url = self._find_video_url_in_mapping(item)
            if direct_url:
                return {
                    "video_url": direct_url,
                    "text": self._extract_text_from_mapping(item),
                    "raw_response": {"items": objects},
                }

            if "message" in item and isinstance(item["message"], dict):
                direct_url = self._find_video_url_in_mapping(item["message"])
                if direct_url:
                    return {
                        "video_url": direct_url,
                        "text": self._extract_text_from_mapping(item["message"]),
                        "raw_response": {"items": objects},
                    }

            fragment = self._extract_text_fragment(item)
            if fragment:
                text_parts.append(fragment)

        merged_text = "\n".join(part for part in text_parts if part).strip()
        url = self._extract_url(merged_text) if merged_text else ""
        result: dict[str, Any] = {"raw_response": {"items": objects}}
        if merged_text:
            result["text"] = merged_text
        if url:
            result["video_url"] = url
        return result

    def _extract_text_fragment(self, data: dict[str, Any]) -> str:
        if "delta" in data and isinstance(data["delta"], dict):
            delta_content = data["delta"].get("content")
            if isinstance(delta_content, str):
                return delta_content

        if "message" in data and isinstance(data["message"], dict):
            content = data["message"].get("content")
            return self._normalize_content_to_text(content)

        if "content" in data:
            return self._normalize_content_to_text(data.get("content"))

        if "text" in data and isinstance(data["text"], str):
            return data["text"]

        return ""

    def _normalize_content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(item.get("url"), str):
                        parts.append(item["url"])
                    elif isinstance(item.get("video_url"), str):
                        parts.append(item["video_url"])
            return "\n".join(parts)

        if isinstance(content, dict):
            return self._extract_text_from_mapping(content)

        return ""

    def _find_video_url_in_mapping(self, data: dict[str, Any]) -> str:
        direct_keys = [
            "video_url",
            "url",
            "result_url",
            "output_url",
            "file_url",
            "download_url",
        ]
        for key in direct_keys:
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        for key, value in data.items():
            if isinstance(value, dict):
                nested = self._find_video_url_in_mapping(value)
                if nested:
                    return nested
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        nested = self._find_video_url_in_mapping(item)
                        if nested:
                            return nested
                    elif isinstance(item, str):
                        url = self._extract_url(item)
                        if url:
                            return url
            elif isinstance(value, str):
                url = self._extract_url(value)
                if url:
                    return url
        return ""

    def _extract_text_from_mapping(self, data: dict[str, Any]) -> str:
        text_candidates: list[str] = []
        for key in ["text", "content", "message", "detail", "description"]:
            value = data.get(key)
            if isinstance(value, str):
                text_candidates.append(value)
            elif isinstance(value, dict):
                normalized = self._normalize_content_to_text(value)
                if normalized:
                    text_candidates.append(normalized)
            elif isinstance(value, list):
                normalized = self._normalize_content_to_text(value)
                if normalized:
                    text_candidates.append(normalized)
        return "\n".join(item for item in text_candidates if item).strip()

    def _extract_url(self, text: str) -> str:
        match = re.search(r"https?://[^\s<>\]\)\"']+", text)
        return match.group(0) if match else ""

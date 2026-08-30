from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import Settings, get_settings
from .errors import AppError, LLMError


def parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        content = "".join(text_parts)
    if not isinstance(content, str):
        raise ValueError("模型没有返回 JSON 文本")

    candidate = content.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(candidate[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型 JSON 顶层必须是对象")
    return value


class BailianChatClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_schema: dict[str, Any] | None = None,
        schema_name: str = "structured_response",
    ) -> str:
        if not self.settings.dashscope_api_key:
            raise AppError(
                "API_KEY_MISSING",
                "服务器未配置 DASHSCOPE_API_KEY。可配置密钥，或设置 MOCK_LLM=true 体验离线演示。",
                status_code=503,
            )

        body: dict[str, Any] = {
            "model": model or self.settings.qwen_text_model,
            "messages": messages,
            "temperature": temperature,
            # qwen-plus aliases across free-tier accounts are not guaranteed
            # to point at a generation that accepts max_completion_tokens.
            # The compatible endpoint still supports max_tokens for all Qwen
            # chat models, so use it for the hackathon fallback chain.
            "max_tokens": max_tokens,
            "enable_thinking": False,
        }
        if response_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            }

        response = await self._post(body)
        if response.status_code >= 400 and response_schema:
            # Some qwen-plus snapshots only expose JSON Object. Keep the output
            # machine-readable while preserving compatibility with free quotas.
            body["response_format"] = {"type": "json_object"}
            response = await self._post(body)

        data = self._decode_response(response)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("百炼没有返回可用结果", details={"response": data})
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        raise LLMError("百炼返回内容格式无法识别")

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any],
        schema_name: str,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        content = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_schema=response_schema,
            schema_name=schema_name,
        )
        try:
            return parse_json_content(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError(
                "模型返回的结构化 JSON 无法解析",
                details={"preview": content[:500]},
            ) from exc

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self.settings.dashscope_base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.llm_timeout_seconds)
            ) as client:
                return await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.settings.dashscope_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"无法连接百炼：{exc}") from exc

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(
                f"百炼返回了非 JSON 响应（HTTP {response.status_code}）"
            ) from exc
        if response.status_code >= 400:
            error = data.get("error") or {}
            message = error.get("message") or data.get("message") or "百炼请求失败"
            raise LLMError(
                str(message),
                details={"status": response.status_code, "code": error.get("code")},
            )
        return data

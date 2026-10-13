"""OpenAI 兼容的聊天补全客户端。

只要服务端实现了 ``POST {base_url}/chat/completions``，就能直接用：
DeepSeek、OpenAI、通义千问、Kimi、Ollama、vLLM、one-api 等都可以。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.errors import LLMError
from app.core.logging import get_logger

logger = get_logger(__name__)


def strip_json_fences(content: str) -> str:
    """去掉大模型爱加的 ```json 代码块围栏。"""

    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_object(content: str) -> dict[str, Any]:
    """从模型输出里尽力提取一个 JSON 对象。"""

    text = strip_json_fences(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"模型返回的内容不是合法 JSON：{text[:200]}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的 JSON 解析失败：{exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMError(f"期望模型返回 JSON 对象，实际是 {type(parsed).__name__}")
    return parsed


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 120.0,
        temperature: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 8192,
        use_json_mode: bool = True,
    ) -> dict[str, Any]:
        """请求模型并返回解析后的 JSON 对象。"""

        if not self.configured:
            raise LLMError("未配置大模型 API Key（AIPR_LLM_API_KEY）")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        content = await self._post_chat(payload, allow_retry_without_json_mode=use_json_mode)
        return parse_json_object(content)

    async def _post_chat(
        self,
        payload: dict[str, Any],
        *,
        allow_retry_without_json_mode: bool,
    ) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint, headers=self._headers(), json=payload
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"请求大模型失败：{exc}") from exc

        # 某些服务端不支持 response_format，去掉后重试一次
        if (
            response.status_code == 400
            and allow_retry_without_json_mode
            and "response_format" in payload
        ):
            logger.warning("服务端拒绝了 response_format，去掉后重试")
            fallback_payload = {key: value for key, value in payload.items() if key != "response_format"}
            return await self._post_chat(fallback_payload, allow_retry_without_json_mode=False)

        if response.status_code >= 400:
            snippet = response.text[:500]
            raise LLMError(f"大模型返回 {response.status_code}：{snippet}")

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"大模型响应不是 JSON：{response.text[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"大模型响应缺少 choices：{str(data)[:300]}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise LLMError("大模型返回了空内容")
        return content

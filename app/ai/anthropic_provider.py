"""Anthropic Claude provider (Messages API)."""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from app.ai.base import AIProvider, AIResponse, ProviderHealth, TextRequest, VisionRequest
from app.ai.http import HTTPClient
from app.core.errors import AIProviderError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"


class AnthropicProvider(AIProvider):
    """Messages API client with vision support."""

    name = "anthropic"
    supports_vision = True
    supports_json_mode = False
    """The API has no JSON mode; JSON is requested through the prompt and
    parsed defensively by :meth:`app.ai.base.AIResponse.json`."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_key = api_key
        headers = {"Content-Type": "application/json", "anthropic-version": API_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        self.client = HTTPClient(
            base_url or DEFAULT_BASE_URL,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            provider=self.name,
        )

    def _require_key(self) -> None:
        if not self.api_key:
            raise AIProviderError(
                "No Anthropic API key configured",
                recovery_action="Add the key in AI Settings; it is stored in the OS credential store.",
            )

    @staticmethod
    def _split_system(request: TextRequest) -> tuple[str, list[dict[str, Any]]]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        turns = [{"role": m.role, "content": m.content} for m in request.messages if m.role != "system"]
        if not turns:
            turns = [{"role": "user", "content": " "}]
        return "\n\n".join(system_parts), turns

    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Call ``/messages``."""
        self._require_key()
        system, turns = self._split_system(request)
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": turns,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system:
            payload["system"] = system
        if request.stop:
            payload["stop_sequences"] = request.stop
        started = time.monotonic()
        data = await self.client.post_json("/messages", payload)
        return self._to_response(data, time.monotonic() - started)

    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Call ``/messages`` with base64 image blocks."""
        self._require_key()
        content: list[dict[str, Any]] = []
        for path in request.image_paths:
            file = Path(path)
            media_type = mimetypes.guess_type(str(file))[0] or "image/png"
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(file.read_bytes()).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": request.prompt})
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.system:
            payload["system"] = request.system
        started = time.monotonic()
        data = await self.client.post_json("/messages", payload)
        return self._to_response(data, time.monotonic() - started)

    async def health_check(self) -> ProviderHealth:
        """Send a one-token message to verify the credentials."""
        started = time.monotonic()
        if not self.api_key:
            return ProviderHealth(self.name, False, "No API key configured")
        try:
            await self.client.post_json(
                "/messages",
                {
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            return ProviderHealth(
                self.name, True, "Messages API reachable", [self.model], time.monotonic() - started
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300], [], time.monotonic() - started)

    def _to_response(self, data: dict[str, Any], latency: float) -> AIResponse:
        blocks = data.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        usage = data.get("usage") or {}
        return AIResponse(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
            latency_seconds=latency,
            raw=data,
        )

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()

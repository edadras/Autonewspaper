"""Local model provider (Ollama / LM Studio / any OpenAI-compatible server).

This is what makes the architecture cloud-optional (specification §36): the
same interfaces work against a model running on the operator's machine, with
no API key and no outbound traffic.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

from app.ai.base import AIProvider, AIResponse, ProviderHealth, TextRequest, VisionRequest
from app.ai.http import HTTPClient

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"


class LocalProvider(AIProvider):
    """Ollama-compatible client.

    ``api_style="ollama"`` (default) talks to ``/api/chat``; ``api_style="openai"``
    talks to ``/v1/chat/completions`` so LM Studio, vLLM and llama.cpp servers
    work unchanged.
    """

    name = "local"
    supports_vision = True
    supports_json_mode = True
    requires_network = False

    def __init__(
        self,
        model: str = "llama3.2",
        *,
        base_url: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 1,
        api_style: str = "ollama",
        api_key: str = "",
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_style = api_style
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = HTTPClient(
            base_url or DEFAULT_BASE_URL,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            provider=self.name,
        )

    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Send a chat completion to the local server."""
        started = time.monotonic()
        if self.api_style == "openai":
            payload: dict[str, Any] = {
                "model": request.model or self.model,
                "messages": [m.to_dict() for m in request.messages],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }
            if request.json_mode:
                payload["response_format"] = {"type": "json_object"}
            data = await self.client.post_json("/v1/chat/completions", payload)
            choices = data.get("choices") or []
            text = choices[0].get("message", {}).get("content", "") if choices else ""
        else:
            payload = {
                "model": request.model or self.model,
                "messages": [m.to_dict() for m in request.messages],
                "stream": False,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                },
            }
            if request.json_mode:
                payload["format"] = "json"
            data = await self.client.post_json("/api/chat", payload)
            text = (data.get("message") or {}).get("content", "")
        return AIResponse(
            text=text or "",
            provider=self.name,
            model=self.model,
            latency_seconds=time.monotonic() - started,
            raw=data,
        )

    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Send images to a local vision model."""
        started = time.monotonic()
        images = [
            base64.b64encode(Path(path).read_bytes()).decode("ascii") for path in request.image_paths
        ]
        if self.api_style == "openai":
            from app.ai.openai_provider import encode_data_url

            content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            content += [
                {"type": "image_url", "image_url": {"url": encode_data_url(Path(p))}}
                for p in request.image_paths
            ]
            messages: list[dict[str, Any]] = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.append({"role": "user", "content": content})
            data = await self.client.post_json(
                "/v1/chat/completions",
                {
                    "model": request.model or self.model,
                    "messages": messages,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                },
            )
            choices = data.get("choices") or []
            text = choices[0].get("message", {}).get("content", "") if choices else ""
        else:
            messages = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.append({"role": "user", "content": request.prompt, "images": images})
            payload: dict[str, Any] = {
                "model": request.model or self.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": request.temperature},
            }
            if request.json_mode:
                payload["format"] = "json"
            data = await self.client.post_json("/api/chat", payload)
            text = (data.get("message") or {}).get("content", "")
        return AIResponse(
            text=text or "",
            provider=self.name,
            model=self.model,
            latency_seconds=time.monotonic() - started,
            raw=data,
        )

    async def health_check(self) -> ProviderHealth:
        """Ask the local server which models it has loaded."""
        started = time.monotonic()
        try:
            if self.api_style == "openai":
                data = await self.client.get_json("/v1/models")
                models = [m.get("id", "") for m in data.get("data", [])]
            else:
                data = await self.client.get_json("/api/tags")
                models = [m.get("name", "") for m in data.get("models", [])]
            return ProviderHealth(
                self.name,
                True,
                f"Local server reachable at {self.client.base_url}",
                models[:50],
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300], [], time.monotonic() - started)

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()

"""OpenAI (and OpenAI-compatible) provider."""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from app.ai.base import AIProvider, AIResponse, ProviderHealth, TextRequest, VisionRequest
from app.ai.http import HTTPClient
from app.core.errors import AIProviderError, ImageGenerationError
from app.models.schemas import GeneratedImage, ImageRequest

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def encode_data_url(path: Path) -> str:
    """Return a ``data:`` URL for a local image file."""
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"


class OpenAIProvider(AIProvider):
    """Chat Completions API client.

    Also works with any OpenAI-compatible endpoint (Azure OpenAI gateways,
    vLLM, LiteLLM, OpenRouter) by overriding ``base_url``.
    """

    name = "openai"
    supports_vision = True
    supports_json_mode = True

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        organization: str | None = None,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_key = api_key
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if organization:
            headers["OpenAI-Organization"] = organization
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
                "No OpenAI API key configured",
                recovery_action="Add the key in AI Settings; it is stored in the OS credential store.",
            )

    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Call ``/chat/completions``."""
        self._require_key()
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [m.to_dict() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if request.stop:
            payload["stop"] = request.stop
        started = time.monotonic()
        data = await self.client.post_json("/chat/completions", payload)
        return self._to_response(data, time.monotonic() - started)

    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Call ``/chat/completions`` with image parts."""
        self._require_key()
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for path in request.image_paths:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": encode_data_url(Path(path)), "detail": request.detail},
                }
            )
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        data = await self.client.post_json("/chat/completions", payload)
        return self._to_response(data, time.monotonic() - started)

    async def generate_image(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Call ``/images/generations`` and write the result to *target*."""
        self._require_key()
        model = str(self.options.get("image_model", "gpt-image-1"))
        payload = {
            "model": model,
            "prompt": request.to_prompt(),
            "size": f"{request.width_px}x{request.height_px}",
            "n": 1,
        }
        data = await self.client.post_json("/images/generations", payload)
        items = data.get("data") or []
        if not items:
            raise ImageGenerationError("OpenAI returned no image", context={"response": str(data)[:400]})
        item = items[0]
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.get("b64_json"):
            target.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            target.write_bytes(await self.client.get_bytes(item["url"]))
        else:
            raise ImageGenerationError("OpenAI image payload had neither b64_json nor url")
        return GeneratedImage(
            path=str(target),
            provider=self.name,
            model=model,
            prompt=request.to_prompt(),
            width=request.width_px,
            height=request.height_px,
            seed=request.seed,
            revised_prompt=item.get("revised_prompt", ""),
        )

    async def health_check(self) -> ProviderHealth:
        """List models to verify the key works."""
        started = time.monotonic()
        if not self.api_key:
            return ProviderHealth(self.name, False, "No API key configured")
        try:
            data = await self.client.get_json("/models")
            models = sorted(item.get("id", "") for item in data.get("data", []))[:50]
            return ProviderHealth(
                self.name, True, f"{len(models)} model(s) reachable", models, time.monotonic() - started
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300], [], time.monotonic() - started)

    def _to_response(self, data: dict[str, Any], latency: float) -> AIResponse:
        choices = data.get("choices") or []
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        usage = data.get("usage") or {}
        return AIResponse(
            text=text or "",
            provider=self.name,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_seconds=latency,
            raw=data,
        )

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()

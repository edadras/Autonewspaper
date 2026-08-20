"""Google Gemini provider (Generative Language API)."""

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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(AIProvider):
    """``generateContent`` client with vision and image generation."""

    name = "gemini"
    supports_vision = True
    supports_json_mode = True

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_key = api_key
        self.client = HTTPClient(
            base_url or DEFAULT_BASE_URL,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            max_retries=max_retries,
            provider=self.name,
        )

    def _require_key(self) -> None:
        if not self.api_key:
            raise AIProviderError(
                "No Gemini API key configured",
                recovery_action="Add the key in AI Settings; it is stored in the OS credential store.",
            )

    def _path(self, model: str, action: str = "generateContent") -> str:
        return f"/models/{model}:{action}?key={self.api_key}"

    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Call ``models/{model}:generateContent``."""
        self._require_key()
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
            if m.role != "system"
        ] or [{"role": "user", "parts": [{"text": " "}]}]
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if request.json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if request.stop:
            payload["generationConfig"]["stopSequences"] = request.stop
        started = time.monotonic()
        data = await self.client.post_json(self._path(request.model or self.model), payload)
        return self._to_response(data, time.monotonic() - started)

    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Call ``generateContent`` with ``inline_data`` image parts."""
        self._require_key()
        parts: list[dict[str, Any]] = [{"text": request.prompt}]
        for path in request.image_paths:
            file = Path(path)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mimetypes.guess_type(str(file))[0] or "image/png",
                        "data": base64.b64encode(file.read_bytes()).decode("ascii"),
                    }
                }
            )
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        started = time.monotonic()
        data = await self.client.post_json(self._path(request.model or self.model), payload)
        return self._to_response(data, time.monotonic() - started)

    async def generate_image(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Generate an image with an image-capable Gemini/Imagen model."""
        self._require_key()
        model = str(self.options.get("image_model", "imagen-3.0-generate-002"))
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        if model.startswith("imagen"):
            payload = {
                "instances": [{"prompt": request.to_prompt()}],
                "parameters": {
                    "sampleCount": 1,
                    "aspectRatio": request.aspect_ratio,
                    "negativePrompt": request.negative_prompt,
                },
            }
            data = await self.client.post_json(self._path(model, "predict"), payload)
            predictions = data.get("predictions") or []
            if not predictions or not predictions[0].get("bytesBase64Encoded"):
                raise ImageGenerationError(
                    "Imagen returned no image", context={"response": str(data)[:400]}
                )
            target.write_bytes(base64.b64decode(predictions[0]["bytesBase64Encoded"]))
        else:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": request.to_prompt()}]}],
                "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
            }
            data = await self.client.post_json(self._path(model), payload)
            blob = self._first_inline_image(data)
            if blob is None:
                raise ImageGenerationError(
                    "Gemini returned no image part", context={"response": str(data)[:400]}
                )
            target.write_bytes(blob)

        return GeneratedImage(
            path=str(target),
            provider=self.name,
            model=model,
            prompt=request.to_prompt(),
            width=request.width_px,
            height=request.height_px,
            seed=request.seed,
        )

    @staticmethod
    def _first_inline_image(data: dict[str, Any]) -> bytes | None:
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        return None

    async def health_check(self) -> ProviderHealth:
        """List models to verify the key works."""
        started = time.monotonic()
        if not self.api_key:
            return ProviderHealth(self.name, False, "No API key configured")
        try:
            data = await self.client.get_json(f"/models?key={self.api_key}")
            models = [m.get("name", "").split("/")[-1] for m in data.get("models", [])][:50]
            return ProviderHealth(
                self.name, True, f"{len(models)} model(s) reachable", models, time.monotonic() - started
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300], [], time.monotonic() - started)

    def _to_response(self, data: dict[str, Any], latency: float) -> AIResponse:
        text = ""
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                text += part.get("text", "")
        usage = data.get("usageMetadata") or {}
        return AIResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=int(usage.get("promptTokenCount", 0)),
            completion_tokens=int(usage.get("candidatesTokenCount", 0)),
            latency_seconds=latency,
            raw=data,
        )

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()

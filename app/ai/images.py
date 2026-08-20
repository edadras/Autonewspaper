"""Image generation providers.

Mirrors :mod:`app.ai.base` for pictures: one abstract interface, several real
back-ends (OpenAI Images, Google Imagen/Gemini, a Stable Diffusion HTTP
server, any OpenAI-compatible local endpoint) and an explicit *disabled*
back-end that marks the slot instead of silently inventing an image.
"""

from __future__ import annotations

import abc
import base64
import logging
import time
from pathlib import Path
from typing import Any

from app.ai.base import ProviderHealth
from app.ai.http import HTTPClient
from app.core.errors import ImageGenerationError
from app.models.schemas import GeneratedImage, ImageRequest
from app.utils import imaging

log = logging.getLogger(__name__)


class ImageGenerationProvider(abc.ABC):
    """Abstract image generator."""

    name: str = "abstract"
    enabled: bool = True

    def __init__(self, model: str, **options: Any) -> None:
        self.model = model
        self.options = options

    @abc.abstractmethod
    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Generate one image for *request* and write it to *target*."""

    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Verify the back-end is reachable."""

    async def close(self) -> None:
        """Release network resources."""

    @staticmethod
    def _finalize(target: Path, generated: GeneratedImage) -> GeneratedImage:
        """Fill in the real pixel dimensions and write the sidecar metadata."""
        info = imaging.image_info(target)
        generated.width = int(info.get("width") or generated.width)
        generated.height = int(info.get("height") or generated.height)
        sidecar = Path(target).with_suffix(Path(target).suffix + ".ai.json")
        from app.utils.files import write_json

        write_json(sidecar, generated.metadata())
        return generated


class OpenAIImageProvider(ImageGenerationProvider):
    """``POST /v1/images/generations``."""

    name = "openai"

    def __init__(
        self,
        model: str = "gpt-image-1",
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 240.0,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_key = api_key
        self.client = HTTPClient(
            base_url or "https://api.openai.com/v1",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            max_retries=2,
            provider="openai-images",
        )

    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Generate and download one image."""
        if not self.api_key:
            raise ImageGenerationError("No OpenAI API key configured for image generation")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = await self.client.post_json(
            "/images/generations",
            {
                "model": self.model,
                "prompt": request.to_prompt(),
                "size": f"{request.width_px}x{request.height_px}",
                "n": 1,
            },
        )
        items = data.get("data") or []
        if not items:
            raise ImageGenerationError("OpenAI returned no image data")
        item = items[0]
        if item.get("b64_json"):
            target.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            target.write_bytes(await self.client.get_bytes(item["url"]))
        else:
            raise ImageGenerationError("OpenAI image payload contained neither b64_json nor url")
        return self._finalize(
            target,
            GeneratedImage(
                path=str(target),
                provider=self.name,
                model=self.model,
                prompt=request.to_prompt(),
                width=request.width_px,
                height=request.height_px,
                seed=request.seed,
                revised_prompt=item.get("revised_prompt", ""),
            ),
        )

    async def health_check(self) -> ProviderHealth:
        """Check the credentials against ``/models``."""
        if not self.api_key:
            return ProviderHealth(self.name, False, "No API key configured")
        started = time.monotonic()
        try:
            await self.client.get_json("/models")
            return ProviderHealth(
                self.name, True, "Images API reachable", [self.model], time.monotonic() - started
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300])

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()


class GeminiImageProvider(ImageGenerationProvider):
    """Google Imagen / image-capable Gemini models."""

    name = "gemini"

    def __init__(
        self,
        model: str = "imagen-3.0-generate-002",
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 240.0,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        from app.ai.gemini_provider import GeminiProvider

        self._delegate = GeminiProvider(
            model, api_key=api_key, base_url=base_url, timeout=timeout, image_model=model
        )
        self.api_key = api_key

    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Delegate to the Gemini provider's image endpoint."""
        generated = await self._delegate.generate_image(request, Path(target))
        return self._finalize(Path(target), generated)

    async def health_check(self) -> ProviderHealth:
        """Reuse the Gemini connectivity check."""
        health = await self._delegate.health_check()
        health.name = self.name
        return health

    async def close(self) -> None:
        """Close the delegate."""
        await self._delegate.close()


class StableDiffusionProvider(ImageGenerationProvider):
    """Stable Diffusion WebUI / ComfyUI compatible HTTP server.

    Targets the AUTOMATIC1111 ``/sdapi/v1/txt2img`` contract, which is also
    implemented by SD.Next and several local inference servers, so the whole
    image pipeline can run without any cloud dependency.
    """

    name = "stablediffusion"

    def __init__(
        self,
        model: str = "sd_xl_base_1.0",
        *,
        base_url: str | None = None,
        timeout: float = 600.0,
        steps: int = 30,
        cfg_scale: float = 6.5,
        sampler: str = "DPM++ 2M Karras",
        api_key: str = "",
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = HTTPClient(
            base_url or "http://127.0.0.1:7860",
            headers=headers,
            timeout=timeout,
            max_retries=1,
            provider="stable-diffusion",
        )
        self.steps = steps
        self.cfg_scale = cfg_scale
        self.sampler = sampler

    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Run txt2img and store the first sample."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "prompt": request.to_prompt(),
            "negative_prompt": request.negative_prompt,
            "width": request.width_px,
            "height": request.height_px,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            "sampler_name": self.sampler,
            "batch_size": 1,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        if self.model:
            payload["override_settings"] = {"sd_model_checkpoint": self.model}
        data = await self.client.post_json("/sdapi/v1/txt2img", payload)
        images = data.get("images") or []
        if not images:
            raise ImageGenerationError("Stable Diffusion server returned no images")
        blob = images[0].split(",", 1)[-1]
        target.write_bytes(base64.b64decode(blob))
        return self._finalize(
            target,
            GeneratedImage(
                path=str(target),
                provider=self.name,
                model=self.model,
                prompt=request.to_prompt(),
                width=request.width_px,
                height=request.height_px,
                seed=request.seed,
            ),
        )

    async def health_check(self) -> ProviderHealth:
        """Query the server for its loaded checkpoints."""
        started = time.monotonic()
        try:
            data = await self.client.get_json("/sdapi/v1/sd-models")
            models = [m.get("model_name", "") for m in (data.get("data") or [])]
            return ProviderHealth(
                self.name,
                True,
                f"Server reachable at {self.client.base_url}",
                models[:50],
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.name, False, str(exc)[:300])

    async def close(self) -> None:
        """Close the HTTP pool."""
        await self.client.aclose()


class LocalImageProvider(ImageGenerationProvider):
    """Any OpenAI-compatible local image endpoint (LocalAI, LiteLLM, vLLM)."""

    name = "local"

    def __init__(
        self,
        model: str = "flux.1-dev",
        *,
        base_url: str | None = None,
        timeout: float = 600.0,
        api_key: str = "",
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self._delegate = OpenAIImageProvider(
            model, api_key=api_key or "local", base_url=base_url or "http://localhost:8080/v1",
            timeout=timeout,
        )
        self._delegate.name = self.name

    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Delegate to the OpenAI-compatible implementation."""
        return await self._delegate.generate(request, target)

    async def health_check(self) -> ProviderHealth:
        """Delegate the connectivity check."""
        health = await self._delegate.health_check()
        health.name = self.name
        return health

    async def close(self) -> None:
        """Close the delegate."""
        await self._delegate.close()


class DisabledImageProvider(ImageGenerationProvider):
    """Image generation turned off.

    Produces a clearly marked grey frame filler and records the reason in the
    asset metadata so the Vision QA stage raises an ``image_missing`` issue and
    the operator can drop a real photograph into the frame. It never pretends
    an image was generated.
    """

    name = "none"
    enabled = False

    def __init__(self, model: str = "disabled", **options: Any) -> None:
        super().__init__(model, **options)

    async def generate(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Write a marked placeholder and flag it in the sidecar metadata."""
        target = Path(target)
        imaging.placeholder(
            target,
            request.width_px,
            request.height_px,
            label=f"NO IMAGE - {request.subject[:48]}",
        )
        generated = GeneratedImage(
            path=str(target),
            provider=self.name,
            model=self.model,
            prompt=request.to_prompt(),
            width=request.width_px,
            height=request.height_px,
        )
        from app.utils.files import write_json

        metadata = generated.metadata()
        metadata.update({"ai_generated": False, "placeholder": True, "reason": "image_generation_disabled"})
        write_json(Path(str(target) + ".ai.json"), metadata)
        log.warning("Image generation disabled - placed a marked placeholder for '%s'", request.subject)
        return generated

    async def health_check(self) -> ProviderHealth:
        """Report the disabled state."""
        return ProviderHealth(self.name, False, "Image generation is disabled in settings")


IMAGE_PROVIDERS: dict[str, type[ImageGenerationProvider]] = {
    "openai": OpenAIImageProvider,
    "gemini": GeminiImageProvider,
    "stablediffusion": StableDiffusionProvider,
    "local": LocalImageProvider,
    "none": DisabledImageProvider,
}

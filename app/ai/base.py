"""Provider-agnostic AI interfaces.

Every AI call in the application goes through :class:`AIProvider`. Nothing
above this module knows which vendor is configured, which is what makes the
provider swappable at runtime and keeps the architecture cloud-optional
(see specification §34 and §36).
"""

from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.core.errors import AIResponseError
from app.models.schemas import GeneratedImage, ImageRequest

log = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    """One turn of a conversation."""

    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        """Vendor-neutral dictionary form."""
        return {"role": self.role, "content": self.content}


@dataclass
class TextRequest:
    """A text completion request."""

    messages: list[ChatMessage]
    temperature: float = 0.4
    max_tokens: int = 4096
    json_mode: bool = False
    """Ask the provider for strict JSON output when it supports it."""
    stop: list[str] = field(default_factory=list)
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def simple(
        cls, system: str, user: str, *, json_mode: bool = False, temperature: float = 0.4
    ) -> TextRequest:
        """Build a two-message request."""
        return cls(
            messages=[ChatMessage("system", system), ChatMessage("user", user)],
            json_mode=json_mode,
            temperature=temperature,
        )


@dataclass
class VisionRequest:
    """An image understanding request."""

    image_paths: list[Path]
    prompt: str
    system: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048
    json_mode: bool = True
    detail: Literal["low", "high", "auto"] = "high"
    model: str | None = None


@dataclass
class AIResponse:
    """A provider reply."""

    text: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_seconds: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion tokens as reported by the provider."""
        return self.prompt_tokens + self.completion_tokens

    def json(self, *, required: bool = True) -> Any:
        """Parse the reply as JSON, tolerating code fences and prose."""
        parsed = extract_json(self.text)
        if parsed is None:
            if required:
                raise AIResponseError(
                    "Provider did not return parsable JSON",
                    context={"provider": self.provider, "model": self.model, "text": self.text[:800]},
                )
            return None
        return parsed


@dataclass
class ProviderHealth:
    """Result of a provider connectivity check."""

    name: str
    available: bool
    detail: str = ""
    models: list[str] = field(default_factory=list)
    latency_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form for the diagnostics page."""
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "models": self.models,
            "latency_seconds": round(self.latency_seconds, 3),
        }


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any | None:
    """Best-effort JSON extraction from a model reply.

    Handles bare JSON, fenced code blocks and JSON embedded in prose. Returns
    ``None`` when nothing parsable is found.
    """
    if not text:
        return None
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    for match in _FENCE_RE.findall(candidate):
        try:
            return json.loads(match.strip())
        except ValueError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            chunk = candidate[start : end + 1]
            try:
                return json.loads(chunk)
            except ValueError:
                # Trailing commas are the most common model mistake.
                repaired = re.sub(r",\s*([}\]])", r"\1", chunk)
                try:
                    return json.loads(repaired)
                except ValueError:
                    continue
    return None


class AIProvider(abc.ABC):
    """Abstract text/vision provider.

    Concrete providers implement :meth:`generate_text` and
    :meth:`analyze_image`. Image *generation* has its own hierarchy in
    :mod:`app.ai.images`; providers that also generate images override
    :meth:`generate_image`.
    """

    name: str = "abstract"
    supports_vision: bool = False
    supports_json_mode: bool = False
    requires_network: bool = True

    def __init__(self, model: str, **options: Any) -> None:
        self.model = model
        self.options = options

    @abc.abstractmethod
    async def generate_text(self, request: TextRequest) -> AIResponse:
        """Return a completion for *request*."""

    async def analyze_image(self, request: VisionRequest) -> AIResponse:
        """Describe or evaluate one or more images."""
        raise NotImplementedError(f"{self.name} does not support vision")

    async def generate_image(self, request: ImageRequest, target: Path) -> GeneratedImage:
        """Generate an image and write it to *target*."""
        raise NotImplementedError(f"{self.name} does not support image generation")

    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Verify credentials and connectivity."""

    async def close(self) -> None:
        """Release network resources."""

    def describe(self) -> dict[str, Any]:
        """Summary used by the AI Settings page."""
        return {
            "name": self.name,
            "model": self.model,
            "vision": self.supports_vision,
            "json_mode": self.supports_json_mode,
            "requires_network": self.requires_network,
        }

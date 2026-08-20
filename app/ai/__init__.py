"""AI provider abstraction, prompt management and the AI service facade."""

from app.ai.base import (
    AIProvider,
    AIResponse,
    ChatMessage,
    ProviderHealth,
    TextRequest,
    VisionRequest,
    extract_json,
)
from app.ai.heuristic_provider import HeuristicProvider
from app.ai.images import IMAGE_PROVIDERS, ImageGenerationProvider
from app.ai.prompts import Prompt, PromptLibrary
from app.ai.registry import TEXT_PROVIDERS, AIService, build_image_provider, build_text_provider

__all__ = [
    "AIProvider",
    "AIResponse",
    "ChatMessage",
    "TextRequest",
    "VisionRequest",
    "ProviderHealth",
    "extract_json",
    "HeuristicProvider",
    "ImageGenerationProvider",
    "IMAGE_PROVIDERS",
    "Prompt",
    "PromptLibrary",
    "AIService",
    "TEXT_PROVIDERS",
    "build_text_provider",
    "build_image_provider",
]

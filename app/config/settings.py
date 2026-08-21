"""Application settings.

Settings are persisted as ``settings.json`` inside the user data directory and
are validated with pydantic. Secrets are *not* part of this model - they are
resolved from :class:`app.config.secrets.SecretStore` at call time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.paths import AppPaths, get_paths
from app.config.secrets import SecretStore

log = logging.getLogger(__name__)

AutomationMode = Literal["auto", "semi_auto", "manual"]
ProviderName = Literal["openai", "anthropic", "gemini", "local", "heuristic"]
VideoProviderName = Literal["higgsfield", "seedance", "http", "none"]
ImageProviderName = Literal["openai", "gemini", "stablediffusion", "local", "none"]


class AISettings(BaseModel):
    """Text/vision model configuration."""

    provider: ProviderName = "heuristic"
    model: str = "gpt-4o-mini"
    vision_provider: ProviderName = "heuristic"
    vision_model: str = "gpt-4o"
    temperature: float = Field(0.4, ge=0.0, le=2.0)
    max_tokens: int = Field(4096, ge=256, le=200_000)
    timeout_seconds: float = Field(120.0, gt=0)
    max_retries: int = Field(3, ge=0, le=10)
    base_url: str | None = None
    """Override endpoint - required for ``local`` (Ollama/vLLM) providers."""

    @field_validator("base_url")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else None


class ImageAISettings(BaseModel):
    """Image generation configuration."""

    provider: ImageProviderName = "none"
    model: str = "gpt-image-1"
    default_style: str = "Realistic Editorial Photography"
    negative_prompt: str = "no text, no watermark, no logo, no signature, no caption"
    base_url: str | None = None
    timeout_seconds: float = Field(240.0, gt=0)
    max_concurrent: int = Field(2, ge=1, le=8)


class VideoAISettings(BaseModel):
    """Video generation configuration.

    The provider list is deliberately open: ``http`` points the generic
    client at any service with the same submit/poll/download shape, so one
    the application has never heard of needs a base URL rather than a code
    change.
    """

    provider: VideoProviderName = "none"
    model: str = "seedance-1-0-pro"
    base_url: str | None = None
    """Overrides the provider's own default; required for ``http``."""
    default_style: str = "cinematic, natural light, shallow depth of field"
    negative_prompt: str = "no text, no watermark, no logo, no distorted faces, no extra limbs"
    default_duration_seconds: float = Field(5.0, gt=0, le=60)
    default_fps: float = Field(24.0, gt=0, le=120)
    quality: str = "high"
    timeout_seconds: float = Field(900.0, gt=0)
    """A clip takes minutes, so this is the §57 ceiling for one generation."""
    poll_seconds: float = Field(5.0, ge=1.0, le=60.0)
    max_concurrent: int = Field(1, ge=1, le=4)
    """These services charge per clip and rate-limit hard; one at a time is
    the honest default."""
    health_path: str = "/models"


class AdobeSettings(BaseModel):
    """Adobe application discovery and control preferences."""

    indesign_path: str | None = None
    photoshop_path: str | None = None
    premiere_path: str | None = None
    indesign_version: str | None = None
    photoshop_version: str | None = None
    premiere_version: str | None = None
    prefer_com: bool = True
    """Use the COM scripting API first (Priority 1)."""
    allow_ui_automation: bool = True
    """Permit the Windows UI Automation fallback (Priority 4)."""
    allow_input_automation: bool = False
    """Permit raw mouse/keyboard automation (Priority 5, last resort)."""
    script_timeout_seconds: float = Field(600.0, gt=0)
    launch_timeout_seconds: float = Field(180.0, gt=0)
    close_documents_on_finish: bool = True
    document_source: Literal["auto", "open_document", "template_file", "new_document"] = "auto"
    """Where the InDesign document the edition is built into comes from.

    ``auto``
        Use the document the operator already has open, if there is one; then
        the template's own InDesign file, if it names one; otherwise create a
        new document. This is the order most people expect.
    ``open_document``
        Only ever build into an open document, and say so plainly when there
        is none rather than quietly making a new one.
    ``template_file``
        Always open the ``.indt``/``.indd`` the template names.
    ``new_document``
        Always start from a blank document, ignoring whatever is open.
    """
    adopt_open_geometry: bool = True
    """Plan the edition to fit the open document's own page setup.

    A document the operator set up by hand rarely matches the template to the
    millimetre. With this on, the page size, margins and columns are read from
    that document and the layout is planned to fit it. With it off, a document
    whose geometry differs from the template is not adopted at all, because
    the plan would not fit the page it was being placed on.
    """


class LayoutSettings(BaseModel):
    """Layout engine tuning."""

    candidates_per_page: int = Field(6, ge=1, le=64)
    max_iterations: int = Field(5, ge=1, le=20)
    qa_timeout_seconds: float = Field(600.0, gt=0)
    """Wall-clock ceiling for the QA loop on a single page (§57).

    The iteration count alone does not bound the work: rendering a page
    through InDesign can take minutes on a heavy document, so a page that is
    not converging is abandoned with its best result so far.
    """
    qa_max_retries: int = Field(3, ge=0, le=10)
    """Consecutive render failures tolerated before the QA loop gives up."""
    qa_threshold: float = Field(90.0, ge=0, le=100)
    min_body_font_pt: float = Field(7.5, gt=0)
    min_image_dpi: float = Field(200.0, gt=0)
    gutter_mm: float = Field(4.0, ge=0)
    allow_ai_layout_proposals: bool = True
    random_seed: int = 20240101


class ExportSettings(BaseModel):
    """PDF/preview export preferences."""

    default_preset: Literal["print", "digital", "web", "high_quality"] = "print"
    preview_dpi: int = Field(110, ge=36, le=600)
    builtin_pdf_dpi: int = Field(200, ge=72, le=600)
    """Resolution the built-in renderer rasterises PDF pages at.

    It only applies when InDesign is unavailable: that PDF is a proof, not a
    press file (it is RGB raster with no colour management, which the export
    reports), so 200 dpi keeps it legible without the cost of 300.
    """
    render_workers: int = Field(4, ge=1, le=16)
    """How many pages the built-in renderer rasterises at once."""
    export_idml: bool = True
    export_indd: bool = True
    export_jpeg_preview: bool = True
    output_dir: str | None = None


class UISettings(BaseModel):
    """User interface preferences."""

    language: Literal["fa", "en", "ar", "tr"] = "fa"
    theme: Literal["dark", "light", "system"] = "dark"
    undo_steps: int = Field(50, ge=10, le=1000)
    autosave_seconds: int = Field(120, ge=0)
    confirm_destructive: bool = True


class PipelineSettings(BaseModel):
    """Autonomous pipeline behaviour."""

    mode: AutomationMode = "semi_auto"
    parallel_workers: int = Field(4, ge=1, le=32)
    agent_max_iterations: int = Field(12, ge=1, le=100)
    agent_timeout_seconds: float = Field(900.0, gt=0)
    agent_max_retries: int = Field(3, ge=0, le=10)
    stop_on_first_error: bool = False
    generate_missing_images: bool = True


class Settings(BaseSettings):
    """Root settings object persisted to ``settings.json``."""

    model_config = SettingsConfigDict(
        env_prefix="AINS_",
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
    )

    ai: AISettings = Field(default_factory=AISettings)
    image_ai: ImageAISettings = Field(default_factory=ImageAISettings)
    video_ai: VideoAISettings = Field(default_factory=VideoAISettings)
    adobe: AdobeSettings = Field(default_factory=AdobeSettings)
    layout: LayoutSettings = Field(default_factory=LayoutSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    ui: UISettings = Field(default_factory=UISettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)

    default_template_id: str = "broadsheet_fa_standard"
    default_fonts: dict[str, str] = Field(
        default_factory=lambda: {
            "headline": "IRANSans",
            "body": "IRANSans",
            "caption": "IRANSans",
            "latin": "Source Serif Pro",
        }
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    telemetry_enabled: bool = False


class SettingsManager:
    """Loads, validates and persists :class:`Settings`.

    The manager owns the only mutable copy of the settings in the process and
    is handed to components through the DI container - no module-level global
    state is used for configuration.
    """

    def __init__(self, paths: AppPaths | None = None, secret_store: SecretStore | None = None) -> None:
        self.paths = paths or get_paths()
        self.secrets = secret_store or SecretStore(self.paths.data / "secrets.vault")
        self._settings = self.load()

    @property
    def settings(self) -> Settings:
        """The current in-memory settings object."""
        return self._settings

    @property
    def file(self) -> Path:
        """Path of the JSON file backing these settings."""
        return self.paths.settings_file

    def load(self) -> Settings:
        """Read ``settings.json``; fall back to defaults when invalid."""
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                return Settings(**data)
            except Exception as exc:
                backup = self.file.with_suffix(".invalid.json")
                try:
                    self.file.replace(backup)
                except OSError:  # pragma: no cover
                    pass
                log.error("settings.json invalid (%s); defaults restored, old file at %s", exc, backup)
        return Settings()

    def save(self) -> Path:
        """Atomically persist the current settings."""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = self._settings.model_dump(mode="json")
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.file)
        return self.file

    def update(self, **section_values: Any) -> Settings:
        """Update whole sections (``ai=..., adobe=...``) and persist."""
        data = self._settings.model_dump()
        for key, value in section_values.items():
            if key not in data:
                raise KeyError(f"Unknown settings section: {key}")
            if isinstance(data[key], dict) and isinstance(value, dict):
                data[key].update(value)
            else:
                data[key] = value
        self._settings = Settings(**data)
        self.save()
        return self._settings

    def set_path(self, dotted: str, value: Any) -> Settings:
        """Update a single value addressed as ``"ai.model"`` and persist."""
        data = self._settings.model_dump()
        node: Any = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            if part not in node:
                raise KeyError(f"Unknown settings path: {dotted}")
            node = node[part]
        if parts[-1] not in node:
            raise KeyError(f"Unknown settings path: {dotted}")
        node[parts[-1]] = value
        self._settings = Settings(**data)
        self.save()
        return self._settings

    def get_path(self, dotted: str, default: Any = None) -> Any:
        """Read a single value addressed as ``"ai.model"``."""
        node: Any = self._settings.model_dump()
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ------------------------------------------------------------- secrets
    def api_key(self, provider: str) -> str | None:
        """Return the API key for *provider* from the secure store."""
        return self.secrets.get(f"{provider}_api_key")

    def set_api_key(self, provider: str, value: str) -> None:
        """Persist the API key for *provider* into the secure store."""
        if value:
            self.secrets.set(f"{provider}_api_key", value)
        else:
            self.secrets.delete(f"{provider}_api_key")

    def output_dir(self) -> Path:
        """Resolved global output directory."""
        configured = self._settings.export.output_dir
        return Path(configured).expanduser() if configured else self.paths.data / "output"

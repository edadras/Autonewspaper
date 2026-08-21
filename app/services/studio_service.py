"""The studio as a service.

Wires the multi-agent crew to the application's own settings, credentials and
event bus, so a request typed into the interface reaches the same specialists
the tests drive, with the same bounds.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.adobe.service import AdobeService
from app.agents.studio import Brief, Studio, StudioRun
from app.agents.studio_tools import StudioContext
from app.config.paths import AppPaths
from app.config.settings import SettingsManager
from app.core.events import EventBus
from app.core.jobs import CancelToken
from app.creative.analyst import ReferenceAnalyst
from app.formats.registry import FormatRegistry
from app.templates.manager import TemplateManager

log = logging.getLogger(__name__)


class StudioService:
    """Runs studio jobs on behalf of the interface."""

    def __init__(
        self,
        paths: AppPaths,
        settings: SettingsManager,
        adobe: AdobeService,
        templates: TemplateManager,
        *,
        ai: Any = None,
        bus: EventBus | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.adobe = adobe
        self.templates = templates
        self.ai = ai
        self.bus = bus
        self.formats = FormatRegistry()

    # ------------------------------------------------------------ running
    def run(
        self,
        brief: Brief,
        *,
        token: CancelToken | None = None,
        ask: bool = True,
        workspace: Path | None = None,
    ) -> StudioRun:
        """Carry out one brief and return everything the crew produced."""
        context = self.context(workspace=workspace, language=brief.language)
        studio = Studio(context, ai=self.ai, bus=self.bus)
        return studio.run(brief, token=token, ask=ask)

    def context(self, *, workspace: Path | None = None, language: str = "fa") -> StudioContext:
        """A studio context wired to this installation."""
        target = Path(workspace) if workspace else self.settings.output_dir() / "studio"
        target.mkdir(parents=True, exist_ok=True)
        return StudioContext(
            workspace=target,
            adobe=self.adobe,
            formats=self.formats,
            analyst=ReferenceAnalyst(self.ai, keyframe_dir=self.paths.cache / "keyframes"),
            video_provider=self._video_provider(),
            bus=self.bus,
            language=language,
            dpi=self.settings.settings.export.builtin_pdf_dpi,
            style_template=self._style_template(language),
        )

    # ---------------------------------------------------------- internals
    def _style_template(self, language: str) -> Any:
        """The template a one-off document inherits its type scale from.

        The one whose language matches, so a Persian job starts from a Persian
        type scale rather than from an English one scaled to fit.
        """
        found = self.templates.discover()
        for spec in found:
            if spec.language == language:
                return spec
        return found[0] if found else None

    def _video_provider(self) -> Any:
        """The configured generative video service, when there is one.

        Built by the same factory the rest of the application uses, so the key
        comes out of the credential vault rather than out of the settings file
        - §33 - and the §57 bounds are the ones the operator configured.
        """
        from app.ai.registry import build_video_provider
        from app.ai.video import DisabledVideoProvider

        try:
            provider = build_video_provider(self.settings)
        except Exception as exc:  # noqa: BLE001 - a misconfigured service is not a crash
            log.warning("The video provider could not be created: %s", exc)
            return None
        if isinstance(provider, DisabledVideoProvider):
            return None
        return provider

    def formats_for(self, medium: str = "") -> list[dict[str, Any]]:
        """The sizes to offer in the interface."""
        items = self.formats.by_medium(medium) if medium else self.formats.all()
        return [
            {
                "id": item.id,
                "name": item.name,
                "medium": item.medium.value,
                "size": item.describe(),
                "aspect": item.aspect_label(),
            }
            for item in items
        ]

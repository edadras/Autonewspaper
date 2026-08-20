"""Prompt management.

Prompts are never hard-coded in the source (specification §35). They live in
``prompts/<group>/<name>.prompt.md`` with a small front-matter block carrying
the version and metadata, and are rendered with ``str.format``-style
placeholders. The library keeps every version on disk so a run can be
reproduced with the exact prompt text it used.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import ConfigurationError

log = logging.getLogger(__name__)

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
SECTION_RE = re.compile(r"^##\s*(system|user)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Prompt:
    """A versioned prompt template with a system and a user section."""

    name: str
    group: str
    version: str
    system: str
    user: str
    path: Path
    task: str = ""
    description: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """``group/name`` identifier."""
        return f"{self.group}/{self.name}"

    def placeholders(self) -> set[str]:
        """Every ``{placeholder}`` used by this prompt."""
        return set(PLACEHOLDER_RE.findall(self.system)) | set(PLACEHOLDER_RE.findall(self.user))

    def render(self, **values: Any) -> tuple[str, str]:
        """Render ``(system, user)``; missing placeholders raise."""
        missing = self.placeholders() - set(values)
        if missing:
            raise ConfigurationError(
                f"Prompt {self.key} is missing value(s): {', '.join(sorted(missing))}",
                context={"prompt": self.key, "version": self.version},
            )
        safe = {key: ("" if value is None else value) for key, value in values.items()}
        return (self._format(self.system, safe), self._format(self.user, safe))

    @staticmethod
    def _format(template: str, values: dict[str, Any]) -> str:
        """Substitute ``{name}`` and unescape the literal braces ``{{``/``}}``.

        Prompt files contain JSON skeletons, so literal braces are written
        doubled exactly as they would be in a format string.
        """

        def _replace(match: re.Match[str]) -> str:
            return str(values.get(match.group(1), match.group(0)))

        rendered = PLACEHOLDER_RE.sub(_replace, template)
        return rendered.replace("{{", "{").replace("}}", "}")


def parse_prompt_file(path: Path) -> Prompt:
    """Parse one ``*.prompt.md`` file."""
    raw = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    match = FRONT_MATTER_RE.match(raw)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line and not line.strip().startswith("#"):
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"')
        raw = raw[match.end() :]

    sections = SECTION_RE.split(raw)
    system, user = "", raw.strip()
    if len(sections) > 1:
        # sections == [preamble, 'system', body, 'user', body, ...]
        pairs = list(zip(sections[1::2], sections[2::2], strict=False))
        for label, body in pairs:
            if label.lower() == "system":
                system = body.strip()
            else:
                user = body.strip()
    return Prompt(
        name=path.name.removesuffix(".prompt.md"),
        group=path.parent.name,
        version=metadata.get("version", "1"),
        system=system,
        user=user,
        path=path,
        task=metadata.get("task", ""),
        description=metadata.get("description", ""),
        metadata=metadata,
    )


class PromptLibrary:
    """Loads, caches and renders the prompt files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, Prompt] = {}
        self._lock = threading.RLock()
        self._loaded = False

    def load(self, force: bool = False) -> int:
        """Scan the prompt directory; returns how many prompts were found."""
        with self._lock:
            if self._loaded and not force:
                return len(self._cache)
            self._cache.clear()
            if self.root.exists():
                for file in sorted(self.root.rglob("*.prompt.md")):
                    try:
                        prompt = parse_prompt_file(file)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Cannot parse prompt %s: %s", file, exc)
                        continue
                    self._cache[prompt.key] = prompt
            else:
                log.warning("Prompt directory %s does not exist", self.root)
            self._loaded = True
            log.info("Loaded %d prompt(s) from %s", len(self._cache), self.root)
            return len(self._cache)

    def get(self, key: str) -> Prompt:
        """Return the prompt identified by ``"group/name"``."""
        self.load()
        with self._lock:
            prompt = self._cache.get(key)
        if prompt is None:
            raise ConfigurationError(
                f"Prompt '{key}' not found under {self.root}",
                recovery_action="Reinstall the application or restore the prompts/ directory.",
            )
        return prompt

    def has(self, key: str) -> bool:
        """Whether *key* exists."""
        self.load()
        with self._lock:
            return key in self._cache

    def keys(self) -> list[str]:
        """Every available prompt key."""
        self.load()
        with self._lock:
            return sorted(self._cache)

    def render(self, key: str, **values: Any) -> tuple[str, str]:
        """Render a prompt to ``(system, user)``."""
        return self.get(key).render(**values)

    def build_request(self, key: str, *, json_mode: bool = True, temperature: float = 0.4, **values: Any):
        """Build a :class:`~app.ai.base.TextRequest` from a prompt.

        The prompt's ``task`` front-matter is copied into the request metadata
        so the offline provider can service the same call structurally.
        """
        from app.ai.base import TextRequest

        prompt = self.get(key)
        system, user = prompt.render(**values)
        request = TextRequest.simple(system, user, json_mode=json_mode, temperature=temperature)
        request.metadata = {
            "task": prompt.task or prompt.name,
            "prompt": prompt.key,
            "prompt_version": prompt.version,
            "data": values.get("_data", {}),
        }
        return request

    def versions(self) -> dict[str, str]:
        """Mapping of prompt key to version, recorded with each pipeline run."""
        self.load()
        with self._lock:
            return {key: prompt.version for key, prompt in sorted(self._cache.items())}

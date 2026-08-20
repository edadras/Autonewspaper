"""Generative video providers.

Every one of these services works the same way: post a prompt, get a job id,
poll until the job finishes, download the file. What differs is the shape of
the request and the names of the fields, so that is all a provider has to
describe - the polling, the retries, the timeouts and the error mapping are
shared.

Three are implemented: Higgsfield and Seedance, and a generic provider that
can be pointed at any service with the same shape by configuration alone, so
a fourth does not need a code change. Nothing here embeds a key: they come
from the secret store like every other credential.

None of these APIs can be exercised from a machine with no account, so the
interface is real and connectable rather than mocked, and the offline
provider says plainly that it cannot generate rather than returning something
that looks like a result.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app.ai.base import ProviderHealth
from app.ai.http import HTTPClient
from app.core.errors import AppError, Component
from app.models.schemas import GeneratedVideo, VideoRequest

log = logging.getLogger(__name__)


class VideoGenerationError(AppError):
    """A video could not be generated."""

    component = Component.AI
    recovery_action = "Check the provider's key and quota in AI Settings, or try a shorter clip."


class VideoGenerationProvider(abc.ABC):
    """Abstract video generator."""

    name: str = "abstract"
    enabled: bool = True
    #: Longest a single generation may take before it is abandoned (§57).
    timeout_seconds: float = 900.0
    poll_seconds: float = 5.0

    def __init__(self, model: str, **options: Any) -> None:
        self.model = model
        self.options = options

    @abc.abstractmethod
    async def submit(self, request: VideoRequest) -> str:
        """Start a generation and return the provider's job id."""

    @abc.abstractmethod
    async def poll(self, job_id: str) -> dict[str, Any]:
        """Return ``{"status": ..., "url": ..., "error": ...}`` for a job.

        ``status`` is one of ``queued``, ``running``, ``done`` or ``failed``.
        """

    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Verify credentials and connectivity."""

    async def close(self) -> None:  # noqa: B027 - not every provider holds a connection
        """Release network resources."""

    async def generate(
        self,
        request: VideoRequest,
        target: Path,
        *,
        on_progress: Any = None,
    ) -> GeneratedVideo:
        """Generate one video and write it to *target*.

        The wait is bounded and reports as it goes, because these calls take
        minutes and an operator watching a progress bar that never moves has
        no way to tell a slow job from a dead one.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        job_id = await self.submit(request)
        log.info("%s accepted job %s", self.name, job_id)

        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            if elapsed > self.timeout_seconds:
                raise VideoGenerationError(
                    f"{self.name} did not finish job {job_id} within {self.timeout_seconds:.0f}s",
                    context={"job": job_id, "provider": self.name},
                    recovery_action="Try a shorter clip, or check the provider's queue.",
                )
            state = await self.poll(job_id)
            status = str(state.get("status") or "running")
            if on_progress is not None:
                on_progress(status, min(0.95, elapsed / max(1.0, self.timeout_seconds)))
            if status == "done":
                url = state.get("url")
                if not url:
                    raise VideoGenerationError(
                        f"{self.name} reported job {job_id} finished but returned no file",
                        context={"job": job_id, "state": state},
                    )
                await self._download(str(url), target)
                return self._finalize(request, target, job_id, state)
            if status == "failed":
                raise VideoGenerationError(
                    f"{self.name} could not generate the clip: {state.get('error') or 'no reason given'}",
                    context={"job": job_id, "provider": self.name},
                )
            await asyncio.sleep(self.poll_seconds)

    async def _download(self, url: str, target: Path) -> Path:
        """Fetch the finished file."""
        raise NotImplementedError(f"{self.name} does not implement downloading")

    def _finalize(
        self, request: VideoRequest, target: Path, job_id: str, state: dict[str, Any]
    ) -> GeneratedVideo:
        """Describe what was produced, measuring the file where possible."""
        video = GeneratedVideo(
            path=str(target),
            provider=self.name,
            model=self.model,
            prompt=request.to_prompt(),
            width=int(state.get("width") or request.width_px),
            height=int(state.get("height") or request.height_px),
            duration_seconds=float(state.get("duration") or request.duration_seconds),
            fps=float(state.get("fps") or request.fps),
            has_audio=bool(state.get("has_audio", request.audio)),
            seed=request.seed,
            job_id=job_id,
            cost=float(state.get("cost") or 0.0),
        )
        from app.utils.files import write_json

        write_json(target.with_suffix(target.suffix + ".ai.json"), video.metadata())
        return video

    def describe(self) -> dict[str, Any]:
        """Summary for the AI Settings page."""
        return {"name": self.name, "model": self.model, "enabled": self.enabled}


class HTTPVideoProvider(VideoGenerationProvider):
    """A provider reached over HTTP, described by a small map of field names.

    Every one of these services has the same shape - submit, poll, download -
    and differs only in what it calls things. Describing that difference as
    data rather than as a subclass is what lets a service the application has
    never heard of be added from the settings file.
    """

    name = "http"

    #: Where each piece of the answer lives, as dotted paths into the JSON.
    #: A path may list alternatives, because these APIs change field names
    #: between versions and both are usually accepted for a while.
    submit_path: str = "/generate"
    poll_path: str = "/jobs/{job_id}"
    job_id_keys: tuple[str, ...] = ("id", "job_id", "data.id", "task_id")
    status_keys: tuple[str, ...] = ("status", "state", "data.status")
    url_keys: tuple[str, ...] = ("output.url", "video_url", "url", "data.url", "result.url")
    error_keys: tuple[str, ...] = ("error", "message", "failure_reason", "data.error")
    #: What each provider's status strings mean to us.
    done_states: tuple[str, ...] = ("done", "succeeded", "success", "completed", "finished")
    failed_states: tuple[str, ...] = ("failed", "error", "cancelled", "canceled", "rejected")

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        timeout: float = 120.0,
        auth_header: str = "Authorization",
        auth_prefix: str = "Bearer ",
        extra_headers: dict[str, str] | None = None,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.api_key = api_key
        self.base_url = base_url or self.default_base_url()
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers[auth_header] = f"{auth_prefix}{api_key}" if auth_prefix else api_key
        self.client = HTTPClient(self.base_url, headers=headers, timeout=timeout, provider=self.name)

    def default_base_url(self) -> str:
        """Where this provider lives, when the settings do not say."""
        return str(self.options.get("base_url") or "")

    # ---------------------------------------------------------- the request
    def payload(self, request: VideoRequest) -> dict[str, Any]:
        """The body this provider expects. Subclasses shape it."""
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.to_prompt(),
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "width": request.width_px,
            "height": request.height_px,
            "fps": request.fps,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.reference_image:
            body["image"] = request.reference_image
        body.update(request.extra)
        return body

    async def submit(self, request: VideoRequest) -> str:
        self._require_key()
        response = await self.client.post_json(self.submit_path, self.payload(request))
        job_id = _first(response, self.job_id_keys)
        if not job_id:
            raise VideoGenerationError(
                f"{self.name} accepted the request but returned no job id",
                context={"response": _trim(response)},
            )
        return str(job_id)

    async def poll(self, job_id: str) -> dict[str, Any]:
        self._require_key()
        response = await self.client.get_json(self.poll_path.format(job_id=job_id))
        raw = str(_first(response, self.status_keys) or "running").lower()
        if raw in self.done_states:
            status = "done"
        elif raw in self.failed_states:
            status = "failed"
        elif raw in ("queued", "pending", "waiting"):
            status = "queued"
        else:
            status = "running"
        return {
            "status": status,
            "raw_status": raw,
            "url": _first(response, self.url_keys),
            "error": _first(response, self.error_keys),
            "duration": _first(response, ("duration", "output.duration")),
            "width": _first(response, ("width", "output.width")),
            "height": _first(response, ("height", "output.height")),
            "fps": _first(response, ("fps", "output.fps")),
            "cost": _first(response, ("cost", "credits", "usage.cost")),
        }

    async def _download(self, url: str, target: Path) -> Path:
        content = await self.client.get_bytes(url)
        if not content:
            raise VideoGenerationError(f"{self.name} returned an empty file for {url}", context={"url": url})
        target.write_bytes(content)
        return target

    async def health_check(self) -> ProviderHealth:
        if not self.api_key:
            return ProviderHealth(
                name=self.name,
                available=False,
                detail="No API key is stored for this provider.",
            )
        started = time.monotonic()
        try:
            await self.client.get_json(self.health_path())
        except Exception as exc:  # noqa: BLE001 - health is a report, not a failure
            return ProviderHealth(name=self.name, available=False, detail=str(exc)[:200])
        return ProviderHealth(
            name=self.name,
            available=True,
            detail=f"{self.base_url} reachable",
            models=[self.model],
            latency_seconds=time.monotonic() - started,
        )

    def health_path(self) -> str:
        """A cheap endpoint that proves the key works."""
        return str(self.options.get("health_path") or "/models")

    def _require_key(self) -> None:
        if not self.api_key:
            raise VideoGenerationError(
                f"No API key is stored for {self.name}",
                recovery_action="Add the key in AI Settings; it is kept in the credential store.",
            )

    async def close(self) -> None:
        await self.client.aclose()


class HiggsfieldProvider(HTTPVideoProvider):
    """Higgsfield (https://higgsfield.ai).

    Motion-led generation: the prompt describes the shot and a separate
    "motion" describes how the camera moves, which is why the request model
    carries that as its own field rather than folding it into the prompt.
    """

    name = "higgsfield"

    def default_base_url(self) -> str:
        return str(self.options.get("base_url") or "https://platform.higgsfield.ai/v1")

    submit_path = "/image2video"
    poll_path = "/jobs/{job_id}"

    def health_path(self) -> str:
        return str(self.options.get("health_path") or "/models")

    def payload(self, request: VideoRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": request.subject,
            "duration": int(round(request.duration_seconds)),
            "aspect_ratio": request.aspect_ratio,
            "quality": self.options.get("quality", "high"),
        }
        if request.motion:
            body["motions"] = [{"id": request.motion}]
        if request.seed is not None:
            body["seed"] = request.seed
        if request.reference_image:
            body["input_images"] = [{"type": "image_url", "image_url": request.reference_image}]
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        body.update(request.extra)
        return body


class SeedanceProvider(HTTPVideoProvider):
    """Seedance (ByteDance), reached through an OpenAI-shaped gateway.

    Seedance is offered through several gateways rather than one official
    endpoint, so the base URL is a setting: point it at whichever one the
    account is on and the rest is the same.
    """

    name = "seedance"

    def default_base_url(self) -> str:
        return str(self.options.get("base_url") or "https://ark.ap-southeast.bytepluses.com/api/v3")

    submit_path = "/contents/generations/tasks"
    poll_path = "/contents/generations/tasks/{job_id}"
    url_keys = ("content.video_url", "video_url", "output.url", "url")
    status_keys = ("status", "state")
    done_states = ("succeeded", "success", "done", "completed")

    def health_path(self) -> str:
        return str(self.options.get("health_path") or "/models")

    def payload(self, request: VideoRequest) -> dict[str, Any]:
        # Seedance takes its parameters as text switches appended to the
        # prompt, which is unusual but is what the API documents.
        switches = [
            f"--ratio {request.aspect_ratio}",
            f"--duration {int(round(request.duration_seconds))}",
            f"--fps {int(round(request.fps))}",
        ]
        if request.seed is not None:
            switches.append(f"--seed {request.seed}")
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"{request.to_prompt()} {' '.join(switches)}"}
        ]
        if request.reference_image:
            content.append({"type": "image_url", "image_url": {"url": request.reference_image}})
        body: dict[str, Any] = {"model": self.model, "content": content}
        body.update(request.extra)
        return body


class DisabledVideoProvider(VideoGenerationProvider):
    """What is configured when no video service is set up.

    It refuses clearly rather than returning a placeholder file, because a
    silent placeholder in a finished edit is worse than an error.
    """

    name = "none"
    enabled = False

    def __init__(self, model: str = "disabled", **options: Any) -> None:
        super().__init__(model, **options)

    async def submit(self, request: VideoRequest) -> str:
        raise VideoGenerationError(
            "Video generation is switched off",
            recovery_action="Choose a provider in AI Settings and add its key.",
        )

    async def poll(self, job_id: str) -> dict[str, Any]:
        return {"status": "failed", "error": "video generation is switched off"}

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name, available=False, detail="Video generation is switched off in settings."
        )


#: Every provider the application knows by name.
VIDEO_PROVIDERS: dict[str, type[VideoGenerationProvider]] = {
    "higgsfield": HiggsfieldProvider,
    "seedance": SeedanceProvider,
    "http": HTTPVideoProvider,
    "none": DisabledVideoProvider,
}


def _first(payload: Any, paths: tuple[str, ...]) -> Any:
    """The first value present at any of *paths*, which may be dotted."""
    for path in paths:
        value: Any = payload
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, ""):
            return value
    return None


def _trim(payload: Any, limit: int = 400) -> str:
    """A short, log-safe rendering of a response body."""
    import json

    try:
        return json.dumps(payload, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return str(payload)[:limit]

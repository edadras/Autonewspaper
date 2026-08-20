"""Generative video providers.

None of these services can be called from a machine with no account, so the
whole submit/poll/download cycle is exercised against a real HTTP server
started here. That checks the thing that actually breaks - the shape of the
request, how a status is read, what happens when a job fails or never
finishes - rather than a mock that agrees with whatever the code does.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.ai.video import (
    VIDEO_PROVIDERS,
    DisabledVideoProvider,
    HiggsfieldProvider,
    HTTPVideoProvider,
    SeedanceProvider,
    VideoGenerationError,
    _first,
)
from app.models.schemas import VideoRequest

pytestmark = pytest.mark.anyio if False else []

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"stand-in for an mp4" * 8


class FakeProviderHandler(BaseHTTPRequestHandler):
    """A provider that behaves the way these services do."""

    #: Set by each test: how many polls before the job is done, and how it ends.
    polls_before_done = 1
    outcome = "done"
    submitted: list[dict] = []
    poll_count = 0

    def log_message(self, *args):  # noqa: A002 - silence the default logging
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).submitted.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        type(self).poll_count = 0
        self._send({"id": "job-123", "status": "queued"})

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.endswith(".mp4"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO_BYTES)))
            self.end_headers()
            self.wfile.write(VIDEO_BYTES)
            return
        if self.path.startswith("/models"):
            self._send({"data": [{"id": "test-model"}]})
            return
        type(self).poll_count += 1
        if type(self).poll_count <= type(self).polls_before_done:
            self._send({"status": "running"})
            return
        if type(self).outcome == "failed":
            self._send({"status": "failed", "error": "the prompt was rejected"})
            return
        host = self.headers.get("Host")
        self._send(
            {
                "status": "succeeded",
                "output": {
                    "url": f"http://{host}/result.mp4",
                    "duration": 5.0,
                    "width": 1080,
                    "height": 1920,
                },
                "fps": 24,
                "cost": 0.4,
            }
        )


@pytest.fixture
def fake_provider():
    """A live server, and a provider pointed at it."""
    FakeProviderHandler.submitted = []
    FakeProviderHandler.poll_count = 0
    FakeProviderHandler.polls_before_done = 1
    FakeProviderHandler.outcome = "done"
    server = HTTPServer(("127.0.0.1", 0), FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, FakeProviderHandler
    finally:
        server.shutdown()
        server.server_close()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------------- the round trip
def test_a_clip_is_submitted_polled_and_downloaded(fake_provider, tmp_path):
    base, handler = fake_provider
    handler.polls_before_done = 2
    provider = HTTPVideoProvider("test-model", api_key="secret", base_url=base)
    provider.poll_seconds = 0.01
    request = VideoRequest(subject="a street at dusk", duration_seconds=5, aspect_ratio="9:16")

    seen: list[tuple[str, float]] = []
    video = _run(
        provider.generate(request, tmp_path / "clip.mp4", on_progress=lambda s, p: seen.append((s, p)))
    )
    _run(provider.close())

    assert (tmp_path / "clip.mp4").read_bytes() == VIDEO_BYTES
    assert video.job_id == "job-123"
    assert video.duration_seconds == 5.0
    assert (video.width, video.height) == (1080, 1920)
    assert video.cost == 0.4
    # It reported as it went, which is what a minutes-long call has to do.
    assert [state for state, _ in seen][:2] == ["running", "running"]
    assert seen[-1][0] == "done"


def test_the_key_is_sent_and_never_written_into_the_body(fake_provider, tmp_path):
    base, handler = fake_provider
    provider = HTTPVideoProvider("test-model", api_key="secret-key", base_url=base)
    provider.poll_seconds = 0.01
    _run(provider.generate(VideoRequest(subject="x"), tmp_path / "c.mp4"))
    _run(provider.close())

    submitted = handler.submitted[0]
    assert submitted["auth"] == "Bearer secret-key"
    assert "secret-key" not in json.dumps(submitted["body"])


def test_provenance_is_written_beside_the_file(fake_provider, tmp_path):
    base, _handler = fake_provider
    provider = HTTPVideoProvider("test-model", api_key="k", base_url=base)
    provider.poll_seconds = 0.01
    _run(provider.generate(VideoRequest(subject="a street"), tmp_path / "clip.mp4"))
    _run(provider.close())

    sidecar = tmp_path / "clip.mp4.ai.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["ai_generated"] is True
    assert data["kind"] == "video"
    assert data["provider"] == "http"


def test_a_rejected_job_is_reported_with_the_reason(fake_provider, tmp_path):
    base, handler = fake_provider
    handler.outcome = "failed"
    provider = HTTPVideoProvider("test-model", api_key="k", base_url=base)
    provider.poll_seconds = 0.01

    with pytest.raises(VideoGenerationError, match="prompt was rejected"):
        _run(provider.generate(VideoRequest(subject="x"), tmp_path / "c.mp4"))
    _run(provider.close())


def test_a_job_that_never_finishes_is_abandoned(fake_provider, tmp_path):
    """§57: bounded in wall-clock time, not only in retries."""
    base, handler = fake_provider
    handler.polls_before_done = 10_000
    provider = HTTPVideoProvider("test-model", api_key="k", base_url=base)
    provider.poll_seconds = 0.01
    provider.timeout_seconds = 0.2

    with pytest.raises(VideoGenerationError, match="did not finish"):
        _run(provider.generate(VideoRequest(subject="x"), tmp_path / "c.mp4"))
    _run(provider.close())


def test_a_missing_key_is_refused_before_the_network_is_touched(tmp_path):
    provider = HTTPVideoProvider("test-model", api_key="", base_url="http://127.0.0.1:1")
    with pytest.raises(VideoGenerationError, match="No API key"):
        _run(provider.generate(VideoRequest(subject="x"), tmp_path / "c.mp4"))


def test_health_reports_rather_than_raising(fake_provider):
    base, _handler = fake_provider
    healthy = _run(HTTPVideoProvider("m", api_key="k", base_url=base).health_check())
    assert healthy.available is True

    unset = _run(HTTPVideoProvider("m", api_key="", base_url=base).health_check())
    assert unset.available is False and "key" in unset.detail.lower()

    unreachable = _run(HTTPVideoProvider("m", api_key="k", base_url="http://127.0.0.1:1").health_check())
    assert unreachable.available is False


# ------------------------------------------------------------ the providers
def test_higgsfield_sends_motion_as_its_own_field():
    provider = HiggsfieldProvider("dop-turbo", api_key="k")
    body = provider.payload(VideoRequest(subject="a street", motion="push_in", duration_seconds=5))
    assert body["motions"] == [{"id": "push_in"}]
    assert body["prompt"] == "a street", "the motion must not be folded into the prompt"
    assert body["duration"] == 5


def test_seedance_sends_its_parameters_as_prompt_switches():
    provider = SeedanceProvider("seedance-1-0-pro", api_key="k")
    body = provider.payload(
        VideoRequest(subject="a street", aspect_ratio="9:16", duration_seconds=8, fps=24, seed=7)
    )
    text = body["content"][0]["text"]
    assert "--ratio 9:16" in text and "--duration 8" in text and "--seed 7" in text


def test_a_reference_image_reaches_each_provider_in_its_own_shape():
    request = VideoRequest(subject="x", reference_image="https://example.invalid/a.png")
    assert HiggsfieldProvider("m", api_key="k").payload(request)["input_images"][0]["image_url"] == (
        "https://example.invalid/a.png"
    )
    content = SeedanceProvider("m", api_key="k").payload(request)["content"]
    assert content[1]["image_url"]["url"] == "https://example.invalid/a.png"


def test_extra_settings_are_passed_through_untouched():
    body = HTTPVideoProvider("m", api_key="k").payload(
        VideoRequest(subject="x", extra={"camera_fixed": True, "watermark": False})
    )
    assert body["camera_fixed"] is True and body["watermark"] is False


def test_every_named_provider_can_be_built():
    for name, factory in VIDEO_PROVIDERS.items():
        provider = factory("model", api_key="k", base_url="https://example.invalid")
        assert provider.name in (name, "abstract")


def test_switching_video_generation_off_refuses_rather_than_faking_it(tmp_path):
    provider = DisabledVideoProvider()
    with pytest.raises(VideoGenerationError, match="switched off"):
        _run(provider.generate(VideoRequest(subject="x"), tmp_path / "c.mp4"))
    assert not (tmp_path / "c.mp4").exists(), "a placeholder clip is worse than an error"


@pytest.mark.parametrize(
    ("payload", "paths", "expected"),
    [
        ({"id": "a"}, ("id", "job_id"), "a"),
        ({"data": {"id": "b"}}, ("id", "data.id"), "b"),
        ({"content": {"video_url": "u"}}, ("content.video_url",), "u"),
        ({"id": ""}, ("id", "job_id"), None),
        ({}, ("id",), None),
    ],
)
def test_a_field_is_found_wherever_the_provider_puts_it(payload, paths, expected):
    assert _first(payload, paths) == expected


def test_no_provider_is_left_calling_itself_abstract():
    """The name goes into every provenance record and every error message."""
    for factory in VIDEO_PROVIDERS.values():
        assert factory.name != "abstract", factory.__name__


# --------------------------------------------------------- through the app
def test_the_service_builds_the_configured_provider(tmp_path):
    from app.ai.registry import build_video_provider
    from app.config.paths import AppPaths
    from app.config.settings import SettingsManager

    paths = AppPaths.resolve(tmp_path / "data").ensure()
    settings = SettingsManager(paths)
    assert build_video_provider(settings).name == "none"

    settings.update(video_ai={"provider": "higgsfield", "model": "dop-turbo"})
    provider = build_video_provider(settings)
    assert provider.name == "higgsfield"
    assert provider.model == "dop-turbo"
    # §57: the wall-clock ceiling comes from the settings, not a constant.
    settings.update(video_ai={"provider": "higgsfield", "timeout_seconds": 123.0})
    assert build_video_provider(settings).timeout_seconds == 123.0


def test_a_request_is_sized_from_a_named_format(tmp_path):
    from app.ai.registry import AIService
    from app.config.paths import AppPaths
    from app.config.settings import SettingsManager

    paths = AppPaths.resolve(tmp_path / "data").ensure()
    service = AIService(SettingsManager(paths))
    try:
        request = service.video_request("a street at dusk", format_id="reel", motion="push in")
        assert (request.width_px, request.height_px) == (1080, 1920)
        assert request.aspect_ratio == "9:16"
        assert request.fps == 30
        assert request.motion == "push in"
    finally:
        service.close()


def test_the_video_provider_is_closed_with_the_service(tmp_path):
    from app.ai.registry import AIService
    from app.config.paths import AppPaths
    from app.config.settings import SettingsManager

    paths = AppPaths.resolve(tmp_path / "data").ensure()
    service = AIService(SettingsManager(paths))
    assert service.video_provider is not None
    service.close()
    service.close()  # idempotent, like every other shutdown path

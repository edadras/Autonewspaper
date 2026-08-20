"""Shared async HTTP plumbing for the cloud providers.

A single helper implements timeouts, bounded retries with exponential
backoff and consistent error mapping, so each provider only has to describe
its own request/response shape.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from app.core.errors import AIProviderError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class HTTPClient:
    """Thin wrapper around :class:`httpx.AsyncClient` with retry semantics."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        provider: str = "http",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers or {},
            timeout=httpx.Timeout(timeout, connect=min(30.0, timeout)),
            follow_redirects=True,
        )

    async def post_json(self, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """POST JSON and return the parsed JSON response."""
        response = await self._request("POST", path, json=payload, **kwargs)
        return self._parse(response)

    async def get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """GET and return the parsed JSON response."""
        response = await self._request("GET", path, **kwargs)
        return self._parse(response)

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        """GET raw bytes (used to download generated images)."""
        response = await self._request("GET", url, **kwargs)
        return response.content

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning(
                    "%s %s %s failed (attempt %d/%d): %s",
                    self.provider, method, path, attempt + 1, self.max_retries + 1, exc,
                )
            else:
                if response.status_code < 400:
                    return response
                if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = self._backoff(attempt, response)
                    log.warning(
                        "%s returned %s; retrying in %.1fs (attempt %d/%d)",
                        self.provider, response.status_code, delay, attempt + 1, self.max_retries + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise AIProviderError(
                    f"{self.provider} HTTP {response.status_code}: {response.text[:500]}",
                    context={"status": response.status_code, "path": path, "provider": self.provider},
                )
            if attempt < self.max_retries:
                await asyncio.sleep(self._backoff(attempt))
        raise AIProviderError(
            f"{self.provider} request failed after {self.max_retries + 1} attempt(s): {last_error}",
            context={"path": path, "provider": self.provider},
            cause=last_error,
        )

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(60.0, float(retry_after))
                except ValueError:
                    pass
        return min(30.0, (2**attempt) + random.uniform(0, 0.75))

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AIProviderError(
                f"{self.provider} returned a non-JSON body", context={"body": response.text[:400]}
            ) from exc
        if not isinstance(data, dict):
            return {"data": data}
        return data

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

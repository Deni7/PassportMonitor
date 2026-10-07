import asyncio
import math
import random
import time
from collections import defaultdict
from email.utils import parsedate_to_datetime

import httpx

from app.domain import ProviderError, Status, utcnow
from app.observability import DURATION, ERRORS, REQUESTS


def backoff(attempt, factor, retry_after=0):
    return max(retry_after, min(300, factor ** (attempt + 1)) * random.uniform(1, 1.2))


def retry_after_seconds(value, fallback):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else fallback
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return fallback


class Gate:
    def __init__(self, global_interval, provider_interval):
        self.global_interval = global_interval
        self.provider_interval = provider_interval
        self.lock = asyncio.Lock()
        self.global_next = 0.0
        self.provider_next = defaultdict(float)

    async def wait(self, provider):
        async with self.lock:
            now = time.monotonic()
            await asyncio.sleep(max(0, self.global_next - now, self.provider_next[provider] - now))
            now = time.monotonic()
            self.global_next = now + self.global_interval
            self.provider_next[provider] = now + self.provider_interval


class Transport:
    def __init__(self, provider, settings, gate, client=None):
        self.provider, self.settings, self.gate = provider, settings, gate
        self.client = client or httpx.AsyncClient(
            timeout=settings.http_timeout, follow_redirects=False
        )
        self.failures = 0
        self.blocked_until = 0.0

    async def get_json(self, url, params=None):
        if time.monotonic() < self.blocked_until:
            raise ProviderError(Status.RATE_LIMIT, "Circuit open; requests suspended")
        for attempt in range(self.settings.max_retries + 1):
            await self.gate.wait(self.provider)
            # Another in-flight request may have opened the circuit while we waited.
            if time.monotonic() < self.blocked_until:
                raise ProviderError(Status.RATE_LIMIT, "Circuit open; requests suspended")
            REQUESTS.labels(self.provider).inc()
            started = time.monotonic()
            retry_after = 0
            try:
                response = await self.client.get(url, params=params)
                if response.status_code in (403, 429, 503):
                    retry_after = retry_after_seconds(
                        response.headers.get("Retry-After", "0"), self.settings.circuit_cooldown
                    )
                    status = Status.RATE_LIMIT
                    if response.status_code == 403:
                        status = (
                            Status.CLOUDFLARE
                            if (
                                "cloudflare" in response.text.lower()
                                or "cf-mitigated" in response.headers
                            )
                            else Status.PROVIDER_ERROR
                        )
                    self.blocked_until = time.monotonic() + max(
                        retry_after, self.settings.circuit_cooldown
                    )
                    raise ProviderError(status, f"HTTP {response.status_code}; requests suspended")
                if response.status_code >= 400 or response.is_redirect:
                    raise ProviderError(Status.PROVIDER_ERROR, f"HTTP {response.status_code}")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        Status.PARSING_ERROR, "Provider contract changed: not JSON"
                    ) from exc
                self.failures = 0
                return data
            except httpx.TimeoutException:
                error = ProviderError(Status.TIMEOUT, "Provider request timed out")
            except httpx.HTTPError:
                error = ProviderError(Status.PROVIDER_ERROR, "Provider connection failed")
            except ProviderError as exc:
                error = exc
            finally:
                DURATION.labels(self.provider).observe(time.monotonic() - started)
            ERRORS.labels(self.provider, error.status).inc()
            self.failures += 1
            if self.failures >= self.settings.circuit_threshold:
                self.blocked_until = max(
                    self.blocked_until, time.monotonic() + self.settings.circuit_cooldown
                )
            if (
                attempt == self.settings.max_retries
                or time.monotonic() < self.blocked_until
                or error.status == Status.PARSING_ERROR
            ):
                raise error
            await asyncio.sleep(backoff(attempt, self.settings.backoff_factor, retry_after))

    async def close(self):
        await self.client.aclose()

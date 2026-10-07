from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.domain import ProviderError, Status
from app.providers.transport import Transport, backoff, retry_after_seconds


def config(**kwargs):
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:", telegram_bot_token="123456:test", **kwargs
    )


async def test_timeout_retry_backoff(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, json={"data": []})

    sleep = AsyncMock()
    monkeypatch.setattr("app.providers.transport.asyncio.sleep", sleep)
    transport = Transport(
        "dmsu",
        config(),
        SimpleNamespace(wait=AsyncMock()),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    assert await transport.get_json("https://cherga.dmsu.gov.ua/test") == {"data": []}
    assert len(calls) == 3 and sleep.await_count == 2
    await transport.close()


@pytest.mark.parametrize("code", [403, 429, 503])
async def test_server_protection_opens_circuit_without_immediate_retries(code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(code, headers={"Retry-After": "900"}, text="cloudflare")

    transport = Transport(
        "dmsu",
        config(circuit_threshold=1),
        SimpleNamespace(wait=AsyncMock()),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(ProviderError):
        await transport.get_json("https://cherga.dmsu.gov.ua/test")
    with pytest.raises(ProviderError):
        await transport.get_json("https://cherga.dmsu.gov.ua/test")
    assert len(calls) == 1
    import time

    assert transport.blocked_until - time.monotonic() > 890
    await transport.close()


async def test_invalid_json_is_not_empty_availability():
    transport = Transport(
        "dmsu",
        config(),
        SimpleNamespace(wait=AsyncMock()),
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
        ),
    )
    with pytest.raises(ProviderError) as error:
        await transport.get_json("https://cherga.dmsu.gov.ua/test")
    assert error.value.status == Status.PARSING_ERROR
    await transport.close()


def test_backoff_respects_retry_after(monkeypatch):
    monkeypatch.setattr("app.providers.transport.random.uniform", lambda *_: 1)
    assert backoff(0, 2) == 2
    assert backoff(1, 2) == 4
    assert backoff(2, 2, 120) == 120


def test_retry_after_http_date_and_malformed_values():
    from datetime import timedelta
    from email.utils import format_datetime

    from app.domain import utcnow

    assert retry_after_seconds(format_datetime(utcnow() + timedelta(seconds=3600)), 300) > 3590
    assert retry_after_seconds("invalid", 300) == 300
    assert retry_after_seconds("inf", 300) == 300

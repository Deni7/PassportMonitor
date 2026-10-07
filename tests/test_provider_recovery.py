import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.config import Settings
from app.domain import ProviderError, Status
from app.providers.document import DocumentProvider
from app.providers.transport import Transport


def config(**kwargs):
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_bot_token="123456:test",
        max_retries=0,
        **kwargs,
    )


@pytest.mark.parametrize("code", [403, 429, 503])
async def test_queued_requests_stop_when_another_request_opens_circuit(code):
    waiting = 0
    all_waiting, first_finished = asyncio.Event(), asyncio.Event()

    async def wait(provider):
        nonlocal waiting
        first = waiting == 0
        waiting += 1
        if waiting == 4:
            all_waiting.set()
        await (all_waiting if first else first_finished).wait()

    handle = Mock(return_value=httpx.Response(code, headers={"Retry-After": "900"}))
    transport = Transport(
        "dmsu",
        config(),
        SimpleNamespace(wait=wait),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    async def request(first):
        try:
            return await transport.get_json("https://cherga.dmsu.gov.ua/test")
        finally:
            if first:
                first_finished.set()

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(request(i == 0) for i in range(4)), return_exceptions=True),
            timeout=5,
        )
        assert all(isinstance(result, ProviderError) for result in results)
        assert all(result.status == Status.RATE_LIMIT for result in results[1:])
        assert handle.call_count == 1
        assert transport.failures == 1
        assert transport.blocked_until - time.monotonic() > 890
    finally:
        await transport.close()


async def test_retry_stops_if_circuit_opens_during_backoff(monkeypatch):
    handle = Mock(side_effect=httpx.ReadTimeout("timeout"))
    settings = config()
    settings.max_retries = 1
    transport = Transport(
        "dmsu",
        settings,
        SimpleNamespace(wait=AsyncMock()),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    async def open_circuit(delay):
        transport.blocked_until = time.monotonic() + 900

    monkeypatch.setattr("app.providers.transport.asyncio.sleep", open_circuit)
    try:
        with pytest.raises(ProviderError) as error:
            await transport.get_json("https://cherga.dmsu.gov.ua/test")
        assert error.value.status == Status.RATE_LIMIT
        assert handle.call_count == 1
        assert transport.failures == 1
    finally:
        await transport.close()


@pytest.fixture
def browser_driver(monkeypatch):
    runtimes = []

    async def start():
        browser = Mock()
        browser.is_connected.return_value = True
        contexts = []

        async def new_context(**kwargs):
            context = Mock()
            handlers = {}
            context.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
            context.route = AsyncMock()

            async def close_context():
                handlers["close"]()

            context.close = AsyncMock(side_effect=close_context)
            contexts.append(context)
            return context

        async def close_browser():
            browser.is_connected.return_value = False
            for context in contexts:
                await context.close()

        browser.new_context = AsyncMock(side_effect=new_context)
        browser.close = AsyncMock(side_effect=close_browser)
        runtime = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
        )
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(
        "app.providers.document.async_playwright",
        Mock(return_value=SimpleNamespace(start=AsyncMock(side_effect=start))),
    )
    return runtimes


async def test_document_recreates_disconnected_browser(browser_driver):
    provider = DocumentProvider(config(), None)
    try:
        await provider._start()
        old_context, old_browser, old_runtime = provider.context, provider.browser, provider.runtime
        provider.pages["catalog"] = Mock()
        provider.pages["working"] = Mock()
        old_browser.is_connected.return_value = False
        # Cleanup can fail after a crash; it must still release the driver and recover.
        old_context.close.side_effect = BrowserError("Target closed")
        old_browser.close.side_effect = BrowserError("Browser closed")
        await provider._start()
        assert provider.context is not old_context
        assert provider.browser is not old_browser
        assert provider.browser.is_connected()
        assert not provider.pages
        assert len(browser_driver) == 2
        old_runtime.stop.assert_awaited_once()
        provider.context.route.assert_awaited_once_with("**/*", provider._route)
    finally:
        await provider.close()


async def test_document_recreates_closed_context_and_reuses_connected_browser(browser_driver):
    provider = DocumentProvider(config(), None)
    try:
        await provider._start()
        old_context, browser, runtime = provider.context, provider.browser, provider.runtime
        provider.pages["working"] = Mock()
        await old_context.close()
        await provider._start()
        context = provider.context
        assert context is not old_context
        assert provider.browser is browser and provider.runtime is runtime
        assert not provider.pages
        assert len(browser_driver) == 1
        runtime.chromium.launch.assert_awaited_once()
        runtime.stop.assert_not_awaited()
        context.route.assert_awaited_once_with("**/*", provider._route)
        # A late close event from the old context must not invalidate the replacement.
        await old_context.close()
        await provider._start()
        assert provider.context is context
    finally:
        await provider.close()


@pytest.mark.parametrize("error", [BrowserTimeout("timeout"), BrowserError("page error")])
async def test_document_keeps_connected_browser_after_operation_error(browser_driver, error):
    provider = DocumentProvider(config(), None)
    try:
        await provider._start()
        context, browser, runtime = provider.context, provider.browser, provider.runtime
        with pytest.raises(ProviderError):
            await provider._operation(AsyncMock(side_effect=error))
        await provider._start()
        assert provider.context is context and provider.browser is browser
        assert len(browser_driver) == 1
        browser.close.assert_not_awaited()
        runtime.stop.assert_not_awaited()
    finally:
        await provider.close()

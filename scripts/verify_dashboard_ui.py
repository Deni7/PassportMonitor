"""Offline UI verification with an isolated database and synthetic records.

Run from the project root: python -m scripts.verify_dashboard_ui
Set PLAYWRIGHT_BROWSERS_PATH if Chromium is stored outside its default cache.
"""

import asyncio
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from playwright.async_api import async_playwright, expect
from pydantic import SecretStr

from app.dashboard import add_dashboard
from app.domain import utcnow
from app.models import (
    Base,
    BotEvent,
    PollJob,
    PollRun,
    PollRunSubscription,
    ProviderHealth,
    ProviderRecord,
    ProviderRequest,
    ServiceRecord,
    Subscription,
    User,
)
from app.repository import Repository


async def verify():
    root = Path(__file__).resolve().parents[1]
    output = root / ".dashboard-qa"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="passport-dashboard-") as directory:
        repo = Repository("sqlite+aiosqlite:///" + str(Path(directory) / "qa.db"))
        try:
            async with repo.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            now = utcnow()
            async with repo.sessions.begin() as session:
                session.add_all(
                    [
                        ProviderRecord(id="dmsu", data={}),
                        ProviderRecord(id="document", data={}),
                        ServiceRecord(id="passport", name="Закордонний паспорт"),
                        User(
                            id=5555555555,
                            chat_id=5555555555,
                            full_name="Тестовий користувач",
                            username="qa_user",
                            last_seen=now,
                        ),
                    ]
                )
            async with repo.sessions.begin() as session:
                session.add_all(
                    [
                        Subscription(
                            id=1,
                            telegram_user_id=5555555555,
                            provider_mode="dmsu",
                            location_id="lviv",
                            location_name="Львів",
                            locations={"dmsu": []},
                            canonical_service_id="passport",
                            date_from=now.date(),
                            date_to=now.date() + timedelta(days=20),
                        ),
                        PollJob(
                            id="dmsu:101:1",
                            provider_id="dmsu",
                            status="AVAILABLE",
                            next_run=now + timedelta(seconds=60),
                            last_success=now,
                            data={
                                "department": {"name": "Львівський відділ"},
                                "service": {"name": "Закордонний паспорт"},
                            },
                        ),
                        ProviderHealth(
                            id="dmsu", last_success=now, response_time=2.1, last_available_slots=2
                        ),
                        ProviderHealth(id="document", last_success=now, response_time=4.2),
                    ]
                )
            async with repo.sessions.begin() as session:
                session.add(
                    PollRun(
                        id=1,
                        job_id="dmsu:101:1",
                        provider_id="dmsu",
                        status="AVAILABLE",
                        started_at=now - timedelta(seconds=2),
                        finished_at=now,
                        duration_ms=2100,
                        slot_count=2,
                        data={
                            "department": {"name": "Львівський відділ"},
                            "service": {"name": "Закордонний паспорт"},
                            "slots": [{"day": now.date().isoformat(), "time": "09:30"}],
                        },
                    )
                )
                for i in range(7):
                    for j in range((i + 1) * 2):
                        created = now - timedelta(days=6 - i, hours=j)
                        session.add(
                            BotEvent(
                                user_id=5555555555,
                                kind="callback" if j % 2 else "command",
                                status="HANDLED",
                                text="add" if j % 2 else "/start",
                                created_at=created,
                                data={"state_before": "Wizard:provider"},
                            )
                        )
                        if j % 3 == 0:
                            session.add(
                                BotEvent(
                                    user_id=5555555555,
                                    kind="outgoing",
                                    status="SENT",
                                    text="Є вільні місця",
                                    created_at=created,
                                    data={"job_id": "dmsu:101:1"},
                                )
                            )
                session.add(
                    BotEvent(
                        user_id=5555555555,
                        kind="message",
                        status="HANDLED",
                        text='<img src=x onerror="alert(1)">',
                        data={},
                    )
                )
            async with repo.sessions.begin() as session:
                session.add(PollRunSubscription(run_id=1, subscription_id=1, user_id=5555555555))
                session.add(
                    ProviderRequest(
                        run_id=1,
                        operation="dates",
                        status="OK",
                        finished_at=now,
                        duration_ms=800,
                        data={
                            "date_from": now.date().isoformat(),
                            "date_to": now.date().isoformat(),
                            "count": 1,
                            "results": [now.date().isoformat()],
                        },
                    )
                )
            app = web.Application()
            add_dashboard(
                app,
                repo,
                SimpleNamespace(
                    dashboard_username="admin", dashboard_password=SecretStr("qa-password")
                ),
            )
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            url = f"http://127.0.0.1:{runner.addresses[0][1]}/dashboard"
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context(
                        http_credentials={"username": "admin", "password": "qa-password"},
                        viewport={"width": 1536, "height": 1200},
                        timezone_id="Europe/Kyiv",
                    )
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "console",
                        lambda message: (
                            errors.append(message.text) if message.type == "error" else None
                        ),
                    )
                    await page.goto(url)
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    await page.locator("#auto-refresh").uncheck()
                    assert await page.locator(".metric").count() == 8
                    assert await page.locator("#error").is_hidden()
                    await page.screenshot(path=str(output / "desktop.png"), full_page=True)
                    for view in (
                        "users",
                        "subscriptions",
                        "runs",
                        "requests",
                        "jobs",
                        "commands",
                        "outgoing",
                        "notifications",
                        "timeline",
                    ):
                        await page.locator(f'nav button[data-view="{view}"]').click()
                        await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                        assert await page.locator("#error").is_hidden(), view
                        if view != "notifications":
                            await page.get_by_role("button", name="Деталі ↗").first.click()
                            assert await page.locator("#detail").is_visible()
                            await page.locator("#close-detail").click()
                    await page.locator('nav button[data-view="users"]').click()
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    await page.get_by_role("button", name="Тестовий користувач", exact=True).click()
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    assert await page.locator("#profile").is_visible()
                    await page.locator('nav button[data-view="timeline"]').click()
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    assert await page.locator("#table-body img").count() == 0
                    await page.locator("#search").fill("<img")
                    await page.wait_for_timeout(500)
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    assert await page.locator("#table-body tr").count() == 1
                    await page.get_by_role("button", name="Деталі ↗").click()
                    assert await page.locator("#detail img").count() == 0
                    await page.locator("#close-detail").click()
                    async with page.expect_download() as download:
                        await page.locator("#export").click()
                    assert (await download.value).suggested_filename.endswith(".csv")
                    await page.locator('nav button[data-view="overview"]').click()
                    await expect(page.locator("#updated")).to_have_text(re.compile("^Оновлено"))
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(output / "mobile.png"), full_page=True)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), "Mobile horizontal overflow"
                    assert not errors, errors
                    await browser.close()
                    print(
                        "Verified all dashboard views, details, user filtering, safe text, CSV, desktop and mobile."
                    )
                    print(f"Synthetic-data screenshots: {output}")
            finally:
                await runner.cleanup()
        finally:
            await repo.close()


if __name__ == "__main__":
    asyncio.run(verify())

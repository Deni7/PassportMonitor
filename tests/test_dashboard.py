import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Message, Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy import select

from app.analytics import IncomingAudit, OutgoingAudit, answer_with_diagnostics, delivery_context
from app.config import Settings
from app.dashboard import add_dashboard
from app.domain import Department, ProviderError, Service, Slot, Status, utcnow
from app.models import BotEvent, PollRun, PollRunSubscription, User
from app.monitoring.notifier import Notifier
from app.monitoring.scheduler import Scheduler

UID = 5555555555
DEP = Department("101", "Львівський відділ", "Адреса", "Львів", "23", "https://cherga.dmsu.gov.ua/")
SERVICE = Service("1", "Паспорт")


@pytest_asyncio.fixture
async def client(repo):
    app = web.Application()
    add_dashboard(
        app,
        repo,
        SimpleNamespace(dashboard_username="admin", dashboard_password=SecretStr("test-password")),
    )
    async with TestClient(
        TestServer(app),
        headers={"Authorization": "Basic " + base64.b64encode(b"admin:test-password").decode()},
    ) as client:
        yield client


async def seed(repo):
    today = utcnow().date()
    sub_id = await repo.add_subscription(
        UID,
        {"key": "lviv", "name": "Львів", "locations": {"dmsu": []}},
        "passport",
        today,
        today + timedelta(days=7),
    )
    provider = SimpleNamespace(
        date_window_days=31,
        get_available_dates=AsyncMock(return_value=[today + timedelta(days=1)]),
        get_available_slots=AsyncMock(
            return_value=[Slot("dmsu", "101", "1", today + timedelta(days=1), "09:30")]
        ),
        get_booking_url=AsyncMock(return_value=DEP.booking_url),
    )
    config = Settings(database_url="sqlite+aiosqlite:///:memory:", telegram_bot_token="123456:test")
    scheduler = Scheduler(
        {"dmsu": provider},
        None,
        repo,
        Notifier(SimpleNamespace(send_message=AsyncMock()), repo, config),
        config,
    )
    await scheduler.run_job("dmsu", "dmsu:101:1", DEP, SERVICE, await repo.subscriptions(UID))
    return scheduler, provider, sub_id


async def test_authentication_and_disabled_dashboard(repo):
    app = web.Application()
    add_dashboard(app, repo, SimpleNamespace(dashboard_password=SecretStr("secret")))
    async with TestClient(TestServer(app)) as client:
        for path in ("/dashboard", "/dashboard/assets/dashboard.js", "/dashboard/api/users"):
            response = await client.get(path)
            assert response.status == 401
            assert "Basic" in response.headers["WWW-Authenticate"]
        response = await client.get(
            "/dashboard",
            headers={"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()},
        )
        assert response.status == 401
    app = web.Application()
    add_dashboard(app, repo, SimpleNamespace())
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/dashboard/api/users")).status == 503


async def test_dashboard_filters_relations_and_results(repo, client):
    _, _, sub_id = await seed(repo)
    response = await client.get("/dashboard")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Аналітика" in await response.text()
    for resource in (
        "overview",
        "users",
        "subscriptions",
        "runs",
        "requests",
        "jobs",
        "events",
        "notifications",
        "timeline",
    ):
        response = await client.get(
            f"/dashboard/api/{resource}?user_id={UID}&subscription_id={sub_id}&provider=dmsu"
        )
        assert response.status == 200, (resource, await response.text())
        data = await response.json()
        if resource == "overview":
            assert data["stats"]["active_subscriptions"] == 1
            assert data["stats"]["runs"] == 1
            assert data["stats"]["slots"] == 1
        elif resource == "runs":
            assert data["items"][0]["data"]["slots"][0]["time"] == "09:30"
            assert data["items"][0]["subscriptions"][0]["subscription_id"] == sub_id
        elif resource == "requests":
            assert data["total"] == 2
            assert data["items"][0]["data"]["count"] == 1
        elif resource == "timeline":
            assert {r["kind"] for r in data["items"]} == {"subscription", "poll"}
    data = await (await client.get("/dashboard/api/runs?user_id=6666666666")).json()
    assert data["total"] == 0
    data = await (await client.get("/dashboard/api/notifications?subscription_id=9999")).json()
    assert data["total"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "from=bad",
        "from=2026-10-07&to=2026-10-06",
        "size=101",
        "page=0",
        "user_id=oops",
        "provider=evil",
        "from=2000-01-01&to=2026-10-07",
    ],
)
async def test_invalid_filters(client, query):
    assert (await client.get("/dashboard/api/runs?" + query)).status == 400


async def test_pagination_and_commands_do_not_include_bot_messages(repo, client):
    async with repo.sessions.begin() as session:
        session.add_all(
            [
                BotEvent(user_id=UID, kind="command", status="HANDLED", text=f"/test{i}")
                for i in range(3)
            ]
        )
        session.add(BotEvent(user_id=UID, kind="outgoing", status="SENT", text="Hello"))
    data = await (
        await client.get("/dashboard/api/events?exclude_outgoing=true&size=2&page=2")
    ).json()
    assert data["total"] == 3 and len(data["items"]) == 1
    assert data["items"][0]["kind"] == "command"


async def test_kyiv_date_boundaries_and_hourly_activity(repo, client):
    async with repo.sessions.begin() as session:
        session.add_all(
            [
                BotEvent(
                    user_id=UID,
                    kind="command",
                    status="HANDLED",
                    text="/included",
                    created_at=datetime(2026, 10, 6, 21, 30, tzinfo=UTC),
                ),
                BotEvent(
                    user_id=UID,
                    kind="command",
                    status="HANDLED",
                    text="/excluded",
                    created_at=datetime(2026, 10, 7, 21, 30, tzinfo=UTC),
                ),
            ]
        )
    data = await (await client.get("/dashboard/api/events?from=2026-10-07&to=2026-10-07")).json()
    assert data["total"] == 1 and data["items"][0]["text"] == "/included"
    assert data["items"][0]["created_at"].endswith("+00:00")
    data = await (await client.get("/dashboard/api/overview?from=2026-10-07&to=2026-10-07")).json()
    assert data["hours"][0] == 1 and sum(data["hours"]) == 1
    assert data["activity"][0]["day"] == "2026-10-07"


async def test_overview_empty_and_read_only_api(client):
    data = await (await client.get("/dashboard/api/overview?user_id=9999")).json()
    assert data["stats"]["users"] == 0 and data["stats"]["avg_duration_ms"] is None
    assert data["funnel"]["notified"] == 0
    assert (await client.post("/dashboard/api/users", json={"blocked": True})).status == 405


async def test_failed_request_preserves_history(repo, client):
    scheduler, provider, _ = await seed(repo)
    from app.models import PollJob

    async with repo.sessions.begin() as session:
        job = await session.get(PollJob, "dmsu:101:1")
        job.next_run = utcnow() - timedelta(seconds=1)
    provider.get_available_dates.side_effect = ProviderError(Status.TIMEOUT, "timeout")
    await scheduler.run_job("dmsu", "dmsu:101:1", DEP, SERVICE, await repo.subscriptions(UID))
    data = await (await client.get("/dashboard/api/runs?status=TIMEOUT")).json()
    assert data["total"] == 1 and data["items"][0]["finished_at"]
    data = await (await client.get("/dashboard/api/requests?status=TIMEOUT")).json()
    assert data["total"] == 1 and data["items"][0]["data"]["error"] == "ProviderError"
    async with repo.sessions() as session:
        assert len((await session.scalars(select(PollRun))).all()) == 2
        assert len((await session.scalars(select(PollRunSubscription))).all()) == 2


async def test_real_dispatcher_captures_profiles_states_and_outgoing(repo):
    bot = Bot("123456:test")
    bot.session.middleware(OutgoingAudit(repo))
    bot.session.make_request = AsyncMock(
        return_value=Message(
            message_id=42, date=utcnow(), chat={"id": UID, "type": "private"}, text="Відповідь"
        )
    )
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(IncomingAudit(repo))
    router = Router()

    @router.message()
    async def handler(message, state):
        await state.set_state("Wizard:dates")
        await message.answer("Відповідь")

    dispatcher.include_router(router)
    update = Update(
        update_id=12,
        message=Message(
            message_id=1,
            date=utcnow(),
            chat={"id": UID, "type": "private"},
            from_user={
                "id": UID,
                "is_bot": False,
                "first_name": "Іван",
                "username": "ivan",
                "language_code": "uk",
            },
            text="/start",
        ),
    )
    try:
        await dispatcher.feed_update(bot, update)
        async with repo.sessions() as session:
            user = await session.get(User, UID)
            assert user.username == "ivan" and user.last_seen
            events = list((await session.scalars(select(BotEvent).order_by(BotEvent.id))).all())
            assert [e.kind for e in events] == ["command", "outgoing"]
            assert events[0].status == "HANDLED"
            assert events[0].data["state_after"] == "Wizard:dates"
            assert events[1].status == "SENT"
            assert events[1].data["message_id"] == 42
            assert events[1].data["source_event_id"] == events[0].id
    finally:
        await bot.session.close()


async def test_outgoing_network_uncertainty_and_batch_link(repo):
    audit = OutgoingAudit(repo)
    method = SendMessage(chat_id=UID, text="Слоти")
    send = AsyncMock(side_effect=TelegramNetworkError(method=method, message="lost"))
    token = delivery_context.set({"notification_ids": [1, 2], "job_id": "dmsu:101:1"})
    try:
        with pytest.raises(TelegramNetworkError):
            await audit(send, None, method)
    finally:
        delivery_context.reset(token)
    async with repo.sessions() as session:
        event = await session.scalar(select(BotEvent))
        assert event.status == "UNCERTAIN"
        assert event.data["notification_ids"] == [1, 2]
        assert event.finished_at


async def test_catalog_warning_persists_diagnostics_without_leaking_context(repo):
    audit = OutgoingAudit(repo)
    details = [
        {
            "provider": "document",
            "status": "RATE_LIMIT",
            "detail": "Browser circuit open",
            "request_sent": False,
            "cause": {
                "status": "RATE_LIMIT",
                "detail": "Browser HTTP 429",
                "http_status": 429,
                "retry_after_seconds": 900,
            },
        }
    ]

    async def answer(text):
        return await audit(
            AsyncMock(return_value=SimpleNamespace(message_id=42)),
            None,
            SendMessage(chat_id=UID, text=text),
        )

    await answer_with_diagnostics(SimpleNamespace(answer=answer), "Каталог недоступний", details)
    assert delivery_context.get() is None
    await audit(
        AsyncMock(return_value=SimpleNamespace(message_id=43)),
        None,
        SendMessage(chat_id=UID, text="Наступне повідомлення"),
    )
    async with repo.sessions() as session:
        events = list((await session.scalars(select(BotEvent).order_by(BotEvent.id))).all())
        assert events[0].data["provider_error_details"] == details
        assert "provider_error_details" not in events[1].data

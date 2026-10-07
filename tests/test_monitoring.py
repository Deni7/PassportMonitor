from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.config import Settings
from app.domain import Department, ProviderError, Service, Slot, Status, utcnow
from app.models import Notification, PollJob, SlotRecord
from app.monitoring.dedup import matches, update_slot
from app.monitoring.notifier import Notifier, format_notification
from app.monitoring.polling import persist_result
from app.monitoring.scheduler import Scheduler, interval
from app.services.catalog import Catalog

DEP = Department(
    "101", "Львівський відділ", "вул. Залізнична, 16", "Львів", "23", "https://cherga.dmsu.gov.ua/"
)
SERVICE = Service("1", "Оформлення паспорта громадянина України для виїзду за кордон")


def settings(**kwargs):
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:", telegram_bot_token="123456:test", **kwargs
    )


async def setup(repo, user=5555555555):
    today = utcnow().date()
    choice = {
        "key": "23:львів",
        "name": "Львів",
        "locations": {
            "dmsu": [{"id": "23:львів", "name": "Львів", "region_id": "23", "city": "Львів"}]
        },
    }
    await repo.add_subscription(user, choice, "passport", today, today + timedelta(days=30))
    return await repo.subscriptions(active=True)


async def result(repo, subscribers, slots):
    async with repo.sessions.begin() as session:
        job = await session.get(PollJob, "dmsu:101:1")
        if not job:
            job = PollJob(
                id="dmsu:101:1",
                provider_id="dmsu",
                data={"department": asdict(DEP), "service": asdict(SERVICE)},
            )
            session.add(job)
            await session.flush()
        await persist_result(session, job, slots, subscribers, DEP, SERVICE, DEP.booking_url, 600)


async def notifications(repo):
    async with repo.sessions() as session:
        return list((await session.scalars(select(Notification))).all())


async def test_restart_dedup_and_overlapping_subscriptions(repo):
    subs = await setup(repo)
    subs = await setup(repo)  # same user overlapping subscriptions
    subs = await setup(repo, 6666666666)
    slot = Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")
    await result(repo, subs, [slot])
    bot = SimpleNamespace(send_message=AsyncMock())
    notifier = Notifier(bot, repo, settings())
    await notifier.send_pending()
    assert bot.send_message.await_count == 2
    assert len(await notifications(repo)) == 2
    # Reopen physical DB connections, preserving committed state across restarts.
    await repo.engine.dispose()
    # New notifier and database sessions simulate a restarted worker.
    await result(repo, subs, [slot])
    await Notifier(bot, repo, settings()).send_pending()
    assert bot.send_message.await_count == 2
    assert all(r.status == "SENT" for r in await notifications(repo))


async def test_shared_polling_fans_out_to_users_and_check_now_uses_cache(repo):
    subs = await setup(repo)
    subs = await setup(repo, 6666666666)
    provider = AsyncMock()
    provider.get_departments.return_value = [DEP]
    provider.get_services.return_value = [SERVICE]
    day = utcnow().date() + timedelta(days=1)
    provider.get_available_dates.return_value = [day]
    provider.get_available_slots.return_value = [Slot("dmsu", "101", "1", day, "09:30")]
    provider.get_booking_url.return_value = DEP.booking_url
    provider.date_window_days = 31
    bot = SimpleNamespace(send_message=AsyncMock())
    catalog = Catalog({"dmsu": provider}, repo, 600)
    scheduler = Scheduler(
        {"dmsu": provider}, catalog, repo, Notifier(bot, repo, settings()), settings()
    )
    targets = await scheduler.collect_targets("dmsu", subs)
    assert len(targets) == 1
    for key, (dep, service, users) in targets.items():
        await scheduler.run_job("dmsu", key, dep, service, list(users.values()))
    assert provider.get_available_dates.await_count == 1
    assert provider.get_available_slots.await_count == 1
    assert len(await notifications(repo)) == 2
    await scheduler.cached_status(subs[0])
    assert provider.get_available_dates.await_count == 1


async def test_one_department_catalog_failure_keeps_other_departments(repo):
    subs = await setup(repo)
    provider = AsyncMock()
    provider.get_departments.return_value = [
        DEP,
        Department("102", "Другий", "Адреса", "Львів", "23", DEP.booking_url),
    ]
    provider.get_services.side_effect = [ProviderError(Status.TIMEOUT, "timeout"), [SERVICE]]
    catalog = Catalog({"dmsu": provider}, repo, 600)
    bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = Scheduler(
        {"dmsu": provider}, catalog, repo, Notifier(bot, repo, settings()), settings()
    )
    targets = await scheduler.collect_targets("dmsu", subs)
    assert list(targets) == ["dmsu:102:1"]


async def test_grouping_dates_and_slots_into_one_message(repo):
    subs = await setup(repo)
    day = utcnow().date() + timedelta(days=1)
    slots = [
        Slot("dmsu", "101", "1", day, "09:30"),
        Slot("dmsu", "101", "1", day, "10:00"),
        Slot("dmsu", "101", "1", day + timedelta(days=1), "11:00"),
    ]
    await result(repo, subs, slots)
    text = format_notification(await notifications(repo))
    assert text.count("📅") == 2 and "09:30" in text and "10:00" in text
    bot = SimpleNamespace(send_message=AsyncMock())
    await Notifier(bot, repo, settings()).send_pending()
    assert bot.send_message.await_count == 1


async def test_pause_cancels_pending_delivery_and_owner_is_checked(repo):
    subs = await setup(repo)
    slot = Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")
    await result(repo, subs, [slot])
    assert not await repo.manage(6666666666, subs[0].id, "pause")
    assert await repo.manage(5555555555, subs[0].id, "pause")
    bot = SimpleNamespace(send_message=AsyncMock())
    await Notifier(bot, repo, settings()).send_pending()
    assert not bot.send_message.called
    assert (await notifications(repo))[0].status == "EXPIRED"


def test_reappearance_cooldown_and_capacity_change():
    now = utcnow()
    row = SimpleNamespace(
        state="DISAPPEARED",
        generation=1,
        disappeared_at=now - timedelta(seconds=20),
        data={"count": 1},
    )
    update_slot(row, {"count": 1}, now, 600)
    assert row.generation == 1
    row.state, row.disappeared_at = "DISAPPEARED", now - timedelta(seconds=700)
    update_slot(row, {"count": 1}, now, 600)
    assert row.generation == 2 and row.state == "AVAILABLE_AGAIN"
    update_slot(row, {"count": 2}, now, 600)
    assert row.generation == 3


async def test_failed_poll_does_not_mark_known_slots_disappeared(repo):
    subs = await setup(repo)
    slot = Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")
    await result(repo, subs, [slot])
    provider = SimpleNamespace(
        get_available_dates=AsyncMock(side_effect=ProviderError(Status.PARSING_ERROR, "changed"))
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = Scheduler(
        {"dmsu": provider}, None, repo, Notifier(bot, repo, settings()), settings()
    )
    await scheduler.run_job("dmsu", "dmsu:101:1", DEP, SERVICE, subs)
    async with repo.sessions() as session:
        state = await session.get(SlotRecord, slot.fingerprint)
        assert state.state == "AVAILABLE"
        assert (await session.get(PollJob, "dmsu:101:1")).status == Status.PARSING_ERROR


async def test_successful_empty_result_marks_disappearance(repo):
    subs = await setup(repo)
    slot = Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")
    await result(repo, subs, [slot])
    await result(repo, subs, [])
    async with repo.sessions() as session:
        assert (await session.get(SlotRecord, slot.fingerprint)).state == "DISAPPEARED"


async def test_definite_telegram_rate_limit_retries_later(repo):
    subs = await setup(repo)
    await result(
        repo, subs, [Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")]
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramRetryAfter(
                method=SendMessage(chat_id=5555555555, text="test"),
                message="limit",
                retry_after=120,
            )
        )
    )
    await Notifier(bot, repo, settings()).send_pending()
    row = (await notifications(repo))[0]
    assert row.status == "PENDING" and row.attempts == 1
    assert (row.next_attempt.replace(tzinfo=utcnow().tzinfo) - utcnow()).total_seconds() > 100


async def test_ambiguous_telegram_delivery_never_retries_automatically(repo):
    subs = await setup(repo)
    await result(
        repo, subs, [Slot("dmsu", "101", "1", utcnow().date() + timedelta(days=1), "09:30")]
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramNetworkError(
                method=SendMessage(chat_id=5555555555, text="test"), message="connection lost"
            )
        )
    )
    notifier = Notifier(bot, repo, settings())
    await notifier.send_pending()
    assert (await notifications(repo))[0].status == "UNCERTAIN"
    await notifier.send_pending()
    assert bot.send_message.await_count == 1


def test_subscription_date_matching():
    day = utcnow().date()
    sub = SimpleNamespace(
        enabled=True, deleted=False, date_from=day, date_to=day + timedelta(days=3)
    )
    assert matches(sub, Slot("dmsu", "101", "1", day))
    assert not matches(sub, Slot("dmsu", "101", "1", day - timedelta(days=1)))
    sub.enabled = False
    assert not matches(sub, Slot("dmsu", "101", "1", day))


def test_fast_window_crosses_midnight(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = settings(dmsu_fast_enabled=True, dmsu_fast_interval=20)
    monkeypatch.setattr("app.monitoring.scheduler.random.uniform", lambda *_: 1)
    assert (
        interval("dmsu", config, datetime(2026, 10, 6, 23, 59, tzinfo=ZoneInfo("Europe/Kyiv")))
        == 20
    )
    assert (
        interval("dmsu", config, datetime(2026, 10, 7, 0, 5, tzinfo=ZoneInfo("Europe/Kyiv"))) == 20
    )
    assert (
        interval("dmsu", config, datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))) == 60
    )

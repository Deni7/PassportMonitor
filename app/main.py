import asyncio
import logging
import time
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text, update

from app.analytics import IncomingAudit, OutgoingAudit
from app.bot import build_router
from app.config import Settings
from app.dashboard import add_dashboard
from app.domain import utcnow
from app.models import (
    BotEvent,
    Notification,
    PollRun,
    ProviderHealth,
    ProviderRecord,
    ProviderRequest,
)
from app.monitoring.notifier import Notifier
from app.monitoring.scheduler import Scheduler
from app.observability import configure_logging
from app.providers.dmsu import DMSUProvider
from app.providers.document import DocumentProvider
from app.providers.transport import Gate, Transport
from app.repository import Repository
from app.services.catalog import Catalog

log = logging.getLogger(__name__)


async def serve_health(repo, scheduler, settings):
    async def health(request):
        try:
            async with repo.sessions() as session:
                await session.execute(text("SELECT 1"))
                records = list((await session.scalars(select(ProviderHealth))).all())
            alive = all(time.monotonic() - v < 600 for v in scheduler.heartbeats.values())
            return web.json_response(
                {
                    "status": "ok" if alive else "worker_stalled",
                    "providers": {
                        r.id: {
                            "last_success": r.last_success.isoformat() if r.last_success else None,
                            "last_error": r.last_error,
                            "consecutive_failures": r.consecutive_failures,
                            "last_available_slots": r.last_available_slots,
                            "response_time": r.response_time,
                        }
                        for r in records
                    },
                },
                status=200 if alive else 503,
            )
        except Exception:
            return web.json_response({"status": "database_unavailable"}, status=503)

    async def metrics(request):
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    application = web.Application()
    add_dashboard(application, repo, settings)
    application.router.add_get("/health", health)
    application.router.add_get("/metrics", metrics)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.metrics_port).start()
    return runner


async def main():
    settings = Settings()
    configure_logging(settings.log_level)
    repo = Repository(settings.database_url)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(repo.close)
        # A session-level advisory lock enforces a single scheduler across replicas. Keep the
        # connection alive for the entire process; a second replica exits before Telegram polling.
        connection = await stack.enter_async_context(repo.engine.connect())
        locked = await connection.scalar(text("SELECT pg_try_advisory_lock(739194621)"))
        if not locked:
            raise RuntimeError("Another monitor instance already owns the database")
        await connection.commit()
        gate = Gate(settings.global_request_interval, settings.provider_request_interval)
        providers = {}
        if settings.dmsu_enabled:
            providers["dmsu"] = DMSUProvider(Transport("dmsu", settings, gate))
        if settings.document_enabled:
            providers["document"] = DocumentProvider(settings, gate)
        for provider in providers.values():
            stack.push_async_callback(provider.close)
        async with repo.sessions.begin() as session:
            for name in providers:
                await session.merge(ProviderRecord(id=name, data={"enabled": True}))
                if await session.get(ProviderHealth, name) is None:
                    session.add(ProviderHealth(id=name))
            # Telegram lacks an idempotency key. A crash after sending but before committing
            # is ambiguous: never silently resend such a batch after restart.
            uncertain = list(
                (
                    await session.scalars(
                        select(Notification).where(Notification.status == "SENDING")
                    )
                ).all()
            )
            for row in uncertain:
                row.status = "UNCERTAIN"
            for model in (PollRun, ProviderRequest):
                await session.execute(
                    update(model)
                    .where(model.status == "RUNNING")
                    .values(status="INTERRUPTED", finished_at=utcnow())
                )
            await session.execute(
                update(BotEvent)
                .where(BotEvent.status == "SENDING")
                .values(status="UNCERTAIN", finished_at=utcnow())
            )
            await session.execute(
                update(BotEvent)
                .where(BotEvent.status == "PROCESSING")
                .values(status="INTERRUPTED", finished_at=utcnow())
            )
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        bot.session.middleware(OutgoingAudit(repo))
        stack.push_async_callback(bot.session.close)
        notifier = Notifier(bot, repo, settings)
        if uncertain:
            log.error(
                "ambiguous_deliveries_after_restart", extra={"fields": {"count": len(uncertain)}}
            )
            await notifier.telegram_alert()
        catalog = Catalog(providers, repo, settings.catalog_ttl)
        scheduler = Scheduler(providers, catalog, repo, notifier, settings)
        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(IncomingAudit(repo))
        dispatcher.include_router(build_router(repo, catalog, scheduler))
        runner = await serve_health(repo, scheduler, settings)
        stack.push_async_callback(runner.cleanup)

        async def notifications():
            while True:
                await notifier.send_pending()
                await asyncio.sleep(1)

        async def lock_keepalive():
            # Fail the process if the advisory-lock connection drops; do not continue without
            # ownership when a replacement process may acquire it.
            while True:
                await asyncio.sleep(30)
                await connection.execute(text("SELECT 1"))
                await connection.commit()

        tasks = [
            asyncio.create_task(dispatcher.start_polling(bot, close_bot_session=False)),
            asyncio.create_task(notifications()),
            asyncio.create_task(lock_keepalive()),
        ]
        tasks += [asyncio.create_task(scheduler.run_provider(p)) for p in providers]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("monitor_stopped", extra={"fields": {"time": utcnow().isoformat()}})


if __name__ == "__main__":
    asyncio.run(main())

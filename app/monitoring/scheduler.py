import asyncio
import logging
import random
import time
from dataclasses import asdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from app.domain import Location, ProviderError, Service, Status, normalize, utcnow
from app.models import (
    PollJob,
    PollRun,
    PollRunSubscription,
    ProviderHealth,
    ProviderRequest,
    SlotRecord,
)
from app.monitoring.dedup import aware
from app.monitoring.polling import persist_result, poll, slot_data
from app.observability import LAST_SUCCESS, SLOTS, SUBSCRIPTIONS
from app.services.display import PROVIDER_TITLES, status_title
from app.services.geography import DOCUMENT_REGIONS
from app.services.mapping import service_matches

log = logging.getLogger(__name__)


def interval(provider, settings, now):
    value = getattr(settings, f"{provider}_poll_interval")
    if provider == "dmsu" and settings.dmsu_fast_enabled:
        local_time = now.astimezone(ZoneInfo("Europe/Kyiv")).time().replace(tzinfo=None)
        start, end = settings.dmsu_fast_start, settings.dmsu_fast_end
        fast = (
            start <= local_time <= end if start <= end else local_time >= start or local_time <= end
        )
        if fast:
            value = settings.dmsu_fast_interval
    return value * random.uniform(1 - settings.jitter, 1 + settings.jitter)


class Scheduler:
    def __init__(self, providers, catalog, repo, notifier, settings):
        self.providers, self.catalog, self.repo = providers, catalog, repo
        self.notifier, self.settings = notifier, settings
        self.heartbeats = {}
        self.cycles = {}
        self.catalog_failures = {}

    async def run_provider(self, provider_name):
        # Independent provider workers share a global HTTP gate. Browser polling is sequential;
        # HTTP jobs have bounded concurrency so one slow department cannot stall the region.
        limit = 1 if provider_name == "document" else self.settings.max_concurrent_jobs
        semaphore = asyncio.Semaphore(limit)

        async def execute(key, department, service, subscribers):
            async with semaphore:
                self.heartbeats[provider_name] = time.monotonic()
                await self.run_job(provider_name, key, department, service, subscribers)

        while True:
            self.heartbeats[provider_name] = time.monotonic()
            subscriptions = await self.repo.subscriptions(active=True)
            SUBSCRIPTIONS.set(len(subscriptions))
            targets = await self.collect_targets(provider_name, subscriptions)
            async with asyncio.TaskGroup() as group:
                for key, (department, service, subs) in targets.items():
                    group.create_task(execute(key, department, service, list(subs.values())))
            self.cycles[provider_name] = time.monotonic()
            await asyncio.sleep(1)

    async def catalog_error(self, provider, scope, error):
        key = (provider, scope)
        if self.catalog_failures.get(key) is not error:
            self.catalog_failures[key] = error
            await self.health(provider, error.status, 0, 0)
            log.warning(
                "catalog_failed", extra={"fields": {"provider": provider, "status": error.status}}
            )

    async def collect_targets(self, provider, subscriptions):
        targets = {}
        for sub in subscriptions:
            for spec in sub.locations.get(provider, []):
                self.heartbeats[provider] = time.monotonic()
                try:
                    departments = await self.catalog.departments(provider, Location(**spec))
                except ProviderError as exc:
                    await self.catalog_error(provider, spec["id"], exc)
                    continue
                for department in departments:
                    self.heartbeats[provider] = time.monotonic()
                    try:
                        services = await self.catalog.services(provider, department)
                    except ProviderError as exc:
                        await self.catalog_error(provider, department.id, exc)
                        continue
                    for service in services:
                        if service_matches(provider, service, sub.canonical_service_id):
                            key = f"{provider}:{department.id}:{service.id}"
                            if key not in targets:
                                targets[key] = (department, service, {})
                            targets[key][2][sub.id] = sub
        return targets

    async def run_job(self, provider_name, key, department, service, subscribers):
        provider = self.providers[provider_name]
        async with self.repo.sessions.begin() as session:
            job = await session.get(PollJob, key)
            if job is None:
                job = PollJob(
                    id=key,
                    provider_id=provider_name,
                    data={"department": asdict(department), "service": asdict(service)},
                    next_run=utcnow(),
                )
                session.add(job)
                await session.flush()
            if aware(job.next_run) > utcnow():
                return
            # Reserve next execution durably. 'Check now' never alters this deadline.
            job.next_run = utcnow() + timedelta(
                seconds=interval(provider_name, self.settings, utcnow())
            )
            run = PollRun(job_id=key, provider_id=provider_name, data={**job.data, "slots": []})
            session.add(run)
            await session.flush()
            run_id = run.id
            session.add_all(
                [
                    PollRunSubscription(
                        run_id=run_id, subscription_id=s.id, user_id=s.telegram_user_id
                    )
                    for s in subscribers
                ]
            )
        started = time.monotonic()
        status, count = Status.PROVIDER_ERROR, 0

        async def observe(operation, params, call):
            async with self.repo.sessions.begin() as session:
                request = ProviderRequest(run_id=run_id, operation=operation, data=params)
                session.add(request)
                await session.flush()
                request_id = request.id
            request_start = time.monotonic()
            request_status, payload = "ERROR", {}
            try:
                values = await call()
                request_status = "OK"
                payload = {
                    "results": [
                        v.isoformat() if operation == "dates" else slot_data(v) for v in values
                    ],
                    "count": len(values),
                }
                return values
            except BaseException as exc:
                request_status = (
                    exc.status
                    if isinstance(exc, ProviderError)
                    else "INTERRUPTED"
                    if isinstance(exc, asyncio.CancelledError)
                    else "ERROR"
                )
                payload = {"error": type(exc).__name__}
                raise
            finally:
                async with self.repo.sessions.begin() as session:
                    request = await session.get(ProviderRequest, request_id)
                    request.status, request.finished_at = request_status, utcnow()
                    request.duration_ms = (time.monotonic() - request_start) * 1000
                    request.data = {**params, **payload}

        try:

            def progress():
                self.heartbeats[provider_name] = time.monotonic()

            result = await poll(provider, department, service, subscribers, progress, observe)
            url = await provider.get_booking_url(department, service)
            async with self.repo.sessions.begin() as session:
                job = await session.get(PollJob, key)
                await persist_result(
                    session,
                    job,
                    result,
                    subscribers,
                    department,
                    service,
                    url,
                    self.settings.reappearance_cooldown,
                )
                job.status = Status.AVAILABLE if result else Status.NO_SLOTS
                job.failures = 0
                job.next_run = utcnow() + timedelta(
                    seconds=interval(provider_name, self.settings, utcnow())
                )
                status, count = job.status, len(result)
                run = await session.get(PollRun, run_id)
                run.data = {**run.data, "slots": [slot_data(s) for s in result]}
            SLOTS.labels(provider_name, key).set(count)
            LAST_SUCCESS.labels(provider_name).set(utcnow().timestamp())
        except ProviderError as exc:
            status = exc.status
            async with self.repo.sessions.begin() as session:
                job = await session.get(PollJob, key)
                job.status, job.failures = status, job.failures + 1
                delay = min(
                    3600,
                    interval(provider_name, self.settings, utcnow())
                    * self.settings.backoff_factor ** min(job.failures, 6),
                )
                job.next_run = utcnow() + timedelta(seconds=delay)
            # Failed/incomplete results never update slots or mark them disappeared.
        except BaseException as exc:
            status = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "ERROR"
            raise
        finally:
            async with self.repo.sessions.begin() as session:
                run = await session.get(PollRun, run_id)
                run.status, run.slot_count = status, count
                run.finished_at = utcnow()
                run.duration_ms = (time.monotonic() - started) * 1000
        duration = time.monotonic() - started
        await self.health(provider_name, status, duration, count)
        log.info(
            "provider_poll",
            extra={
                "fields": {
                    "provider": provider_name,
                    "department": department.id,
                    "service": service.id,
                    "duration_ms": round(duration * 1000),
                    "status": status,
                    "slots": count,
                }
            },
        )

    async def health(self, provider, status, duration, count):
        alert = False
        async with self.repo.sessions.begin() as session:
            record = await session.get(ProviderHealth, provider, with_for_update=True)
            if record is None:
                record = ProviderHealth(id=provider, consecutive_failures=0)
                session.add(record)
            record.response_time = duration
            if status in (Status.AVAILABLE, Status.NO_SLOTS):
                record.last_success, record.consecutive_failures = utcnow(), 0
                record.last_available_slots = count
            else:
                record.last_error = status
                record.consecutive_failures += 1
                urgent = status in (
                    Status.PARSING_ERROR,
                    Status.CLOUDFLARE,
                    Status.CAPTCHA,
                    Status.AUTH_REQUIRED,
                    Status.BROWSER_ERROR,
                )
                due = (
                    record.last_alert is None
                    or (utcnow() - aware(record.last_alert)).total_seconds()
                    >= self.settings.admin_cooldown
                )
                if due and (
                    urgent or record.consecutive_failures >= self.settings.admin_failure_threshold
                ):
                    record.last_alert = utcnow()
                    alert = True
        if alert:
            for admin in self.settings.admin_telegram_ids:
                try:
                    await self.notifier.bot.send_message(
                        admin,
                        f"⚠️ Провайдер {provider}: {status}. Моніторинг тимчасово недоступний.",
                    )
                except TelegramAPIError:
                    log.error("admin_notification_failed", extra={"fields": {"provider": provider}})

    async def cached_status(self, sub):
        messages = []
        async with self.repo.sessions() as session:
            for provider in sub.locations:
                health = await session.get(ProviderHealth, provider)
                if health and health.consecutive_failures:
                    messages.append(
                        f"⚠️ {PROVIDER_TITLES[provider]}: {status_title(health.last_error)}; поточні дані недоступні."
                    )
            jobs = list((await session.scalars(select(PollJob))).all())
            for job in jobs:
                if job.provider_id not in sub.locations:
                    continue
                dep, service = job.data["department"], job.data["service"]
                if not service_matches(
                    job.provider_id, Service(**service), sub.canonical_service_id
                ):
                    continue

                def belongs(spec, dep=dep, provider=job.provider_id):
                    if spec["city"] and normalize(spec["city"]) != normalize(dep["city"]):
                        return False
                    if provider == "dmsu":
                        return spec["region_id"] == dep["location_id"]
                    return (
                        not spec["region_id"]
                        or DOCUMENT_REGIONS.get(dep["city"]) == spec["region_id"]
                    )

                if not any(belongs(s) for s in sub.locations[job.provider_id]):
                    continue
                slots = list(
                    (
                        await session.scalars(
                            select(SlotRecord).where(
                                SlotRecord.job_id == job.id, SlotRecord.state != "DISAPPEARED"
                            )
                        )
                    ).all()
                )
                count = sum(
                    sub.date_from.isoformat() <= r.data["day"] <= sub.date_to.isoformat()
                    for r in slots
                )
                stamp = (
                    aware(job.last_success).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M:%S")
                    if job.last_success
                    else "ще не перевірено"
                )
                messages.append(
                    f"{dep['name']}: {status_title(job.status)}; місць у кеші: {count}; успішна перевірка: {stamp}"
                )
        return messages or ["Першу перевірку заплановано. Запит не змінює частоту опитування."]

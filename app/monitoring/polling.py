from dataclasses import asdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.domain import utcnow
from app.models import Notification, SlotRecord
from app.monitoring.dedup import matches, update_slot


def slot_data(slot):
    return {**asdict(slot), "day": slot.day.isoformat()}


async def poll(provider, department, service, subscribers, progress=lambda: None, observe=None):
    async def request(operation, params, call):
        return await observe(operation, params, call) if observe else await call()

    start = max(
        utcnow().astimezone(ZoneInfo("Europe/Kyiv")).date(),
        min(s.date_from for s in subscribers),
    )
    end = max(s.date_to for s in subscribers)
    result = []
    # Month-sized windows reflect the observed frontend query and avoid unbounded payloads.
    while start <= end:
        progress()
        window_days = getattr(provider, "date_window_days", 31)
        window_end = min(end, start + timedelta(days=window_days - 1)) if window_days else end
        days = await request(
            "dates",
            {"date_from": start.isoformat(), "date_to": window_end.isoformat()},
            lambda start=start, window_end=window_end: provider.get_available_dates(
                department, service, start, window_end
            ),
        )
        for day in days:
            progress()
            if any(s.date_from <= day <= s.date_to for s in subscribers):
                result.extend(
                    await request(
                        "slots",
                        {"day": day.isoformat()},
                        lambda day=day: provider.get_available_slots(department, service, day),
                    )
                )
        start = window_end + timedelta(days=1)
    return list({s.fingerprint: s for s in result}.values())


async def persist_result(session, job, slots, subscribers, department, service, url, cooldown):
    now = utcnow()
    old = {
        r.fingerprint: r
        for r in (
            await session.scalars(select(SlotRecord).where(SlotRecord.job_id == job.id))
        ).all()
    }
    current = {s.fingerprint for s in slots}
    # Only disappear within the current polling date coverage. Shrinking subscriptions is not
    # evidence of upstream disappearance; retain other state for restart-safe deduplication.
    for record in old.values():
        day = date.fromisoformat(record.data["day"])
        covered = any(s.date_from <= day <= s.date_to for s in subscribers)
        if covered and record.fingerprint not in current and record.state != "DISAPPEARED":
            record.state, record.disappeared_at = "DISAPPEARED", now
    for slot in slots:
        data = slot_data(slot)
        record = old.get(slot.fingerprint)
        if record is None:
            record = SlotRecord(
                fingerprint=slot.fingerprint,
                job_id=job.id,
                state="NEW",
                generation=1,
                last_seen=now,
                data=data,
            )
            session.add(record)
        else:
            update_slot(record, data, now, cooldown)
        # Multiple overlapping subscriptions of the same user still yield one delivery.
        for user_id in {s.telegram_user_id for s in subscribers if matches(s, slot)}:
            existing = await session.scalar(
                select(Notification).where(
                    Notification.telegram_user_id == user_id,
                    Notification.fingerprint == slot.fingerprint,
                    Notification.generation == record.generation,
                )
            )
            if existing is None:
                session.add(
                    Notification(
                        telegram_user_id=user_id,
                        fingerprint=slot.fingerprint,
                        generation=record.generation,
                        job_id=job.id,
                        data={
                            "slot": data,
                            "department": asdict(department),
                            "service": asdict(service),
                            "url": url,
                            "detected_at": now.isoformat(),
                            "subscription_ids": [
                                s.id
                                for s in subscribers
                                if s.telegram_user_id == user_id and matches(s, slot)
                            ],
                        },
                    )
                )
                await session.flush()
            elif existing.status in ("PENDING", "EXPIRED"):
                existing.data = {
                    **existing.data,
                    "subscription_ids": [
                        s.id
                        for s in subscribers
                        if s.telegram_user_id == user_id and matches(s, slot)
                    ],
                }
                existing.status = "PENDING"
        if record.state == "NEW":
            record.state = "AVAILABLE"
    job.last_success = now

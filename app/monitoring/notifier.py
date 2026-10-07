import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.analytics import delivery_context
from app.domain import official_url, utcnow
from app.models import Notification, SlotRecord, Subscription, User
from app.observability import NOTIFICATIONS

log = logging.getLogger(__name__)


def format_notification(rows):
    data = rows[0].data
    dep, service = data["department"], data["service"]
    provider = "ДМСУ" if data["slot"]["provider"] == "dmsu" else "ДП «Документ»"
    detected = datetime.fromisoformat(data["detected_at"]).astimezone(ZoneInfo("Europe/Kyiv"))
    header = (
        f"🟢 Є вільні місця\n\nУстанова: {provider}\nПідрозділ: {dep['name']}\n"
        f"Місто: {dep['city']}\nАдреса: {dep['address']}\nПослуга: {service['name']}\n\n"
    )
    # Length is bounded by the notifier's batch size, preserving every slot.
    lines = []
    day = None
    for row in sorted(rows, key=lambda r: (r.data["slot"]["day"], r.data["slot"]["time"] or "")):
        slot = row.data["slot"]
        if day != slot["day"]:
            day = slot["day"]
            lines.append(datetime.fromisoformat(day).strftime("📅 %d.%m.%Y"))
        count = f" — місць: {slot['count']}" if slot["count"] is not None else ""
        lines.append(" • " + (slot["time"] or "час не вказаний") + count)
    return header + "\n".join(lines) + "\n\nВиявлено: " + detected.strftime("%d.%m.%Y %H:%M:%S")


class Notifier:
    def __init__(self, bot, repo, settings):
        self.bot, self.repo, self.settings = bot, repo, settings
        self.lock = asyncio.Lock()
        self.last_telegram_alert = None

    async def send_pending(self):
        async with self.lock, self.repo.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Notification)
                        .where(
                            Notification.status == "PENDING", Notification.next_attempt <= utcnow()
                        )
                        .order_by(Notification.id)
                        .limit(1000)
                    )
                ).all()
            )
            groups = defaultdict(list)
            today = utcnow().astimezone(ZoneInfo("Europe/Kyiv")).date()
            for row in rows:
                slot = await session.get(SlotRecord, row.fingerprint)
                active = await session.scalar(
                    select(Subscription.id).where(
                        Subscription.id.in_(row.data["subscription_ids"]),
                        Subscription.enabled.is_(True),
                        Subscription.deleted.is_(False),
                        Subscription.date_to >= today,
                    )
                )
                if (
                    not active
                    or not slot
                    or slot.state == "DISAPPEARED"
                    or slot.generation != row.generation
                    or date.fromisoformat(row.data["slot"]["day"]) < today
                ):
                    row.status = "EXPIRED"
                    continue
                groups[(row.telegram_user_id, row.job_id)].append(row)
            await session.commit()
            for (user_id, _), group in groups.items():
                user = await session.get(User, user_id)
                if not user or user.blocked:
                    for row in group:
                        row.status = "BLOCKED"
                    await session.commit()
                    continue
                while group:
                    batch = group[:40]
                    while len(format_notification(batch)) > 4000 and len(batch) > 1:
                        batch = batch[: len(batch) // 2]
                    content = format_notification(batch)
                    if len(content) > 4000:
                        content = content[:3999]
                    group = group[len(batch) :]
                    data = batch[0].data
                    url = official_url(data["slot"]["provider"], data["url"])
                    for row in batch:
                        row.status = "SENDING"
                    await session.commit()
                    context_token = delivery_context.set(
                        {
                            "notification_ids": [r.id for r in batch],
                            "job_id": batch[0].job_id,
                            "subscription_ids": sorted(
                                {s for r in batch for s in r.data["subscription_ids"]}
                            ),
                        }
                    )
                    try:
                        await self.bot.send_message(
                            user.chat_id,
                            content,
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(text="Перейти до запису", url=url)]
                                ]
                            ),
                        )
                    except TelegramForbiddenError:
                        user.blocked = True
                        for row in batch + group:
                            row.status = "BLOCKED"
                        await session.commit()
                        break
                    except TelegramNetworkError:
                        # The response may be lost after Telegram accepts a message. No
                        # automatic resend: preserve uncertainty and alert the administrator.
                        for row in batch:
                            row.status = "UNCERTAIN"
                        await session.commit()
                        await self.telegram_alert()
                        break
                    except TelegramAPIError as exc:
                        delay = (
                            exc.retry_after
                            if isinstance(exc, TelegramRetryAfter)
                            else min(3600, 2 ** min(12, batch[0].attempts + 1))
                        )
                        for row in batch:
                            row.status = "PENDING"
                            row.attempts += 1
                            row.next_attempt = utcnow() + timedelta(seconds=delay)
                        await session.commit()
                        log.error(
                            "telegram_delivery_failed", extra={"fields": {"retry_seconds": delay}}
                        )
                        await self.telegram_alert()
                        break
                    else:
                        for row in batch:
                            row.status, row.sent_at = "SENT", utcnow()
                            slot = await session.get(
                                SlotRecord, row.fingerprint, populate_existing=True
                            )
                            if slot.generation == row.generation and slot.state != "DISAPPEARED":
                                slot.state = "NOTIFIED"
                        await session.commit()
                        NOTIFICATIONS.inc()
                    finally:
                        delivery_context.reset(context_token)
                    await asyncio.sleep(0.05)

    async def telegram_alert(self):
        now = utcnow()
        if (
            self.last_telegram_alert
            and (now - self.last_telegram_alert).total_seconds() < self.settings.admin_cooldown
        ):
            return
        self.last_telegram_alert = now
        for admin in self.settings.admin_telegram_ids:
            try:
                await self.bot.send_message(
                    admin,
                    "⚠️ Telegram API: помилка доставки. Перевірте notifications: PENDING буде повторено, UNCERTAIN потребує ручної перевірки доставки.",
                )
            except TelegramAPIError:
                log.error("admin_telegram_delivery_failed")

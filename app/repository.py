from dataclasses import asdict
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import utcnow
from app.models import (
    BotEvent,
    DepartmentRecord,
    LocationRecord,
    ProviderRecord,
    ProviderService,
    ServiceRecord,
    Subscription,
    User,
)


class Repository:
    def __init__(self, url):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def upsert_catalog(self, provider, locations=(), departments=(), services=()):
        async with self.sessions.begin() as session:
            await session.merge(ProviderRecord(id=provider, data={"enabled": True}))
            for location in locations:
                await session.merge(
                    LocationRecord(
                        id=f"{provider}:{location.id}", provider_id=provider, data=asdict(location)
                    )
                )
            for dep in departments:
                await session.merge(
                    DepartmentRecord(
                        id=f"{provider}:{dep.id}", provider_id=provider, data=asdict(dep)
                    )
                )
            for dep, service, canonical, title in services:
                await session.merge(ServiceRecord(id=canonical, name=title))
                await session.merge(
                    ProviderService(
                        id=f"{provider}:{dep.id}:{service.id}:{canonical}",
                        provider_id=provider,
                        canonical_service_id=canonical,
                        data={"department": dep.id, **asdict(service)},
                    )
                )

    async def user(self, user_id, chat_id):
        async with self.sessions.begin() as session:
            existing = await session.get(User, user_id)
            if existing:
                existing.chat_id, existing.blocked = chat_id, False
            else:
                session.add(User(id=user_id, chat_id=chat_id))

    async def service_title(self, service_id):
        async with self.sessions() as session:
            service = await session.get(ServiceRecord, service_id)
            return service.name if service else "Обрана послуга"

    async def add_subscription(self, user_id, choice, service_id, start, end):
        async with self.sessions.begin() as session:
            sub = Subscription(
                telegram_user_id=user_id,
                provider_mode="+".join(sorted(choice["locations"])),
                location_id=choice["key"],
                location_name=choice["name"],
                locations=choice["locations"],
                canonical_service_id=service_id,
                date_from=start,
                date_to=end,
            )
            session.add(sub)
            await session.flush()
            session.add(
                BotEvent(
                    user_id=user_id,
                    kind="subscription",
                    status="CREATED",
                    text=f"Створено моніторинг #{sub.id}",
                    data={
                        "subscription_id": sub.id,
                        "location": sub.location_name,
                        "service": service_id,
                        "date_from": start.isoformat(),
                        "date_to": end.isoformat(),
                    },
                )
            )
            return sub.id

    async def subscriptions(self, user_id=None, active=False):
        query = select(Subscription).where(Subscription.deleted.is_(False))
        if user_id is not None:
            query = query.where(Subscription.telegram_user_id == user_id)
        if active:
            query = query.where(
                Subscription.enabled.is_(True),
                Subscription.date_to >= utcnow().astimezone(ZoneInfo("Europe/Kyiv")).date(),
            )
        async with self.sessions() as session:
            return list((await session.scalars(query.order_by(Subscription.id))).all())

    async def manage(self, user_id, sub_id, action):
        async with self.sessions.begin() as session:
            sub = await session.get(Subscription, sub_id)
            if not sub or sub.telegram_user_id != user_id or sub.deleted:
                return False
            if action == "delete":
                sub.deleted, sub.enabled = True, False
            elif action == "pause":
                sub.enabled = False
            elif action == "resume":
                sub.enabled = True
            else:
                return False
            session.add(
                BotEvent(
                    user_id=user_id,
                    kind="subscription",
                    status=action.upper(),
                    text=f"Моніторинг #{sub.id}: {action}",
                    data={"subscription_id": sub.id},
                )
            )
            return True

    async def close(self):
        await self.engine.dispose()

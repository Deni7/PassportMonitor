"""Authenticated, read-only dashboard on the existing aiohttp listener."""

import base64
import binascii
import hmac
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import String, cast, distinct, func, literal, or_, select, union_all

from app.domain import utcnow
from app.models import (
    BotEvent,
    Notification,
    PollJob,
    PollRun,
    PollRunSubscription,
    ProviderHealth,
    ProviderRequest,
    ServiceRecord,
    Subscription,
    User,
)

STATIC = Path(__file__).with_name("static")
KYIV = ZoneInfo("Europe/Kyiv")


def serialize(row):
    return {
        column.name: (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()
        if isinstance(value, datetime)
        else value.isoformat()
        if isinstance(value, date)
        else value
        for column in row.__table__.columns
        for value in [getattr(row, column.name)]
    }


class Filters:
    def __init__(self, query):
        try:
            today = utcnow().astimezone(KYIV).date()
            start = date.fromisoformat(query.get("from", (today - timedelta(days=6)).isoformat()))
            end = date.fromisoformat(query.get("to", today.isoformat()))
            if end < start or (end - start).days > 365:
                raise ValueError()
            self.start = datetime.combine(start, time.min, KYIV).astimezone(UTC)
            self.end = datetime.combine(end + timedelta(days=1), time.min, KYIV).astimezone(UTC)
            self.user = int(query["user_id"]) if query.get("user_id") else None
            self.subscription = (
                int(query["subscription_id"]) if query.get("subscription_id") else None
            )
            self.page = int(query.get("page", 1))
            self.size = int(query.get("size", 25))
            if self.page < 1 or not 1 <= self.size <= 100:
                raise ValueError()
        except (ValueError, OverflowError):
            raise web.HTTPBadRequest(
                text="Некоректні фільтри: дати до 366 днів, page ≥ 1, size 1–100."
            ) from None
        self.provider = query.get("provider")
        if self.provider and self.provider not in ("dmsu", "document"):
            raise web.HTTPBadRequest(text="Невідомий провайдер")
        self.status, self.kind = query.get("status"), query.get("kind")
        self.search = query.get("search", "")[:200]

    def period(self, column):
        return (column >= self.start, column < self.end)

    def run_scope(self):
        scope = []
        if self.user is not None or self.subscription is not None:
            membership = select(PollRunSubscription.run_id)
            if self.user is not None:
                membership = membership.where(PollRunSubscription.user_id == self.user)
            if self.subscription is not None:
                membership = membership.where(
                    PollRunSubscription.subscription_id == self.subscription
                )
            scope.append(PollRun.id.in_(membership))
        if self.provider:
            scope.append(PollRun.provider_id == self.provider)
        return scope

    def subscriptions(self):
        scope = []
        if self.user is not None:
            scope.append(Subscription.telegram_user_id == self.user)
        if self.subscription is not None:
            scope.append(Subscription.id == self.subscription)
        if self.provider:
            scope.append(Subscription.provider_mode.contains(self.provider))
        return scope

    def events(self):
        scope = list(self.period(BotEvent.created_at))
        if self.user is not None:
            scope.append(BotEvent.user_id == self.user)
        # Provider/subscription filters narrow events to users with matching subscriptions.
        if self.provider or self.subscription is not None:
            scope.append(
                BotEvent.user_id.in_(
                    select(Subscription.telegram_user_id).where(*self.subscriptions())
                )
            )
        return scope


async def paginated(session, query, model, filters, order):
    total = await session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = (
        await session.scalars(
            query.order_by(order, model.id.desc())
            .offset((filters.page - 1) * filters.size)
            .limit(filters.size)
        )
    ).all()
    return {
        "items": [serialize(row) for row in rows],
        "total": total,
        "page": filters.page,
        "size": filters.size,
    }


async def overview(repo, f):
    active = (
        Subscription.enabled.is_(True),
        Subscription.deleted.is_(False),
        Subscription.date_to >= utcnow().astimezone(KYIV).date(),
    )
    runs = select(PollRun).where(*f.period(PollRun.started_at), *f.run_scope()).subquery()
    events = select(BotEvent).where(*f.events()).subquery()
    subs = select(Subscription).where(*f.subscriptions()).subquery()
    user_scope = []
    if f.user is not None:
        user_scope.append(User.id == f.user)
    if f.provider or f.subscription is not None:
        user_scope.append(User.id.in_(select(subs.c.telegram_user_id)))
    async with repo.sessions() as session:

        async def count(query):
            return await session.scalar(query) or 0

        stats = {
            "users": await count(select(func.count(User.id)).where(*user_scope)),
            "active_users": await count(
                select(func.count(distinct(events.c.user_id))).where(
                    events.c.kind.in_(["command", "callback", "message"])
                )
            ),
            "active_subscriptions": await count(
                select(func.count(Subscription.id)).where(*f.subscriptions(), *active)
            ),
            "runs": await count(select(func.count()).select_from(runs)),
            "running": await count(
                select(func.count()).select_from(runs).where(runs.c.status == "RUNNING")
            ),
            "errors": await count(
                select(func.count())
                .select_from(runs)
                .where(runs.c.status.not_in(["AVAILABLE", "NO_SLOTS", "RUNNING"]))
            ),
            "slots": await count(select(func.coalesce(func.sum(runs.c.slot_count), 0))),
            "messages_sent": await count(
                select(func.count())
                .select_from(events)
                .where(events.c.kind == "outgoing", events.c.status == "SENT")
            ),
            "commands": await count(
                select(func.count())
                .select_from(events)
                .where(events.c.kind.in_(["command", "callback"]))
            ),
            "avg_duration_ms": await session.scalar(select(func.avg(runs.c.duration_ms))),
        }
        grouped = await session.execute(
            select(
                runs.c.provider_id, runs.c.status, func.count(), func.avg(runs.c.duration_ms)
            ).group_by(runs.c.provider_id, runs.c.status)
        )
        providers = [
            {"provider": p, "status": s, "count": c, "duration_ms": d} for p, s, c, d in grouped
        ]
        # Hour buckets stay timezone-correct in PostgreSQL and SQLite without dialect SQL.
        activity = {}
        hours = [0] * 24
        hour = (
            func.to_char(func.timezone("UTC", events.c.created_at), 'YYYY-MM-DD"T"HH24')
            if repo.engine.dialect.name == "postgresql"
            else func.substr(cast(events.c.created_at, String), 1, 13)
        )
        grouped_events = await session.execute(
            select(hour, events.c.kind, func.count()).group_by(hour, events.c.kind)
        )
        for stamp, kind, count_value in grouped_events:
            local = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo("UTC")).astimezone(KYIV)
            day = local.date().isoformat()
            bucket = activity.setdefault(
                day, {"day": day, "incoming": 0, "outgoing": 0, "subscription": 0}
            )
            bucket[
                "outgoing"
                if kind == "outgoing"
                else "subscription"
                if kind == "subscription"
                else "incoming"
            ] += count_value
            if kind in ("command", "callback", "message"):
                hours[local.hour] += count_value
        commands = await session.execute(
            select(events.c.text, func.count())
            .where(events.c.kind.in_(["command", "callback"]))
            .group_by(events.c.text)
            .order_by(func.count().desc())
            .limit(10)
        )
        locations = await session.execute(
            select(subs.c.location_name, func.count())
            .group_by(subs.c.location_name)
            .order_by(func.count().desc())
            .limit(10)
        )
        services = await session.execute(
            select(ServiceRecord.name, func.count())
            .join(subs, subs.c.canonical_service_id == ServiceRecord.id)
            .group_by(ServiceRecord.name)
            .order_by(func.count().desc())
            .limit(10)
        )
        funnel = {
            "interacted": stats["active_users"],
            "created": await count(
                select(func.count(distinct(events.c.user_id))).where(
                    events.c.kind == "subscription", events.c.status == "CREATED"
                )
            ),
            "notified": await count(
                select(func.count(distinct(events.c.user_id))).where(
                    events.c.kind == "outgoing",
                    events.c.status == "SENT",
                    events.c.data["job_id"].as_string().is_not(None),
                )
            ),
        }
        health_query = select(ProviderHealth)
        if f.provider:
            health_query = health_query.where(ProviderHealth.id == f.provider)
        health = (await session.scalars(health_query)).all()
        stage = func.coalesce(
            events.c.data["state_before"].as_string(), events.c.data["state_after"].as_string()
        )
        stages = await session.execute(
            select(stage, func.count(), func.count(distinct(events.c.user_id)))
            .where(events.c.kind.in_(["command", "callback", "message"]), stage.is_not(None))
            .group_by(stage)
            .order_by(func.count().desc())
        )
        behavior = {
            "new_users": await count(
                select(func.count(User.id)).where(*user_scope, *f.period(User.created_at))
            ),
            "blocked_users": await count(
                select(func.count(User.id)).where(*user_scope, User.blocked.is_(True))
            ),
            "interacted_without_active_subscription": await count(
                select(func.count(User.id)).where(
                    *user_scope,
                    User.id.in_(
                        select(events.c.user_id).where(
                            events.c.kind.in_(["command", "callback", "message"])
                        )
                    ),
                    User.id.not_in(select(Subscription.telegram_user_id).where(*active)),
                )
            ),
            "new_subscriptions": await count(
                select(func.count())
                .select_from(events)
                .where(events.c.kind == "subscription", events.c.status == "CREATED")
            ),
            "paused": await count(
                select(func.count())
                .select_from(events)
                .where(events.c.kind == "subscription", events.c.status == "PAUSE")
            ),
            "deleted": await count(
                select(func.count())
                .select_from(events)
                .where(events.c.kind == "subscription", events.c.status == "DELETE")
            ),
        }
        return {
            "stats": stats,
            "providers": providers,
            "activity": sorted(activity.values(), key=lambda x: x["day"]),
            "commands": [dict(label=t, count=c) for t, c in commands],
            "locations": [dict(label=t, count=c) for t, c in locations],
            "services": [dict(label=t, count=c) for t, c in services],
            "funnel": funnel,
            "hours": hours,
            "stages": [dict(state=s, count=c, users=u) for s, c, u in stages],
            "behavior": behavior,
            "health": [serialize(h) for h in health],
            "generated_at": utcnow().isoformat(),
        }


def add_dashboard(application, repo, settings):
    @web.middleware
    async def authenticate(request, handler):
        if not request.path.startswith("/dashboard"):
            return await handler(request)
        secret = getattr(settings, "dashboard_password", None)
        if not secret or not secret.get_secret_value():
            raise web.HTTPServiceUnavailable(
                text="Дешборд вимкнено. Задайте DASHBOARD_PASSWORD у .env."
            )
        username = getattr(settings, "dashboard_username", "admin")
        valid = False
        try:
            scheme, encoded = request.headers.get("Authorization", "").split(" ", 1)
            supplied_user, password = (
                base64.b64decode(encoded, validate=True).decode().split(":", 1)
            )
            valid = (
                scheme.lower() == "basic"
                and hmac.compare_digest(supplied_user.encode(), username.encode())
                and hmac.compare_digest(password.encode(), secret.get_secret_value().encode())
            )
        except (ValueError, UnicodeError, binascii.Error):
            pass
        if not valid:
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'Basic realm="Passport Monitor", charset="UTF-8"'},
                text="Потрібна авторизація адміністратора",
            )
        response = await handler(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    application.middlewares.append(authenticate)

    async def index(request):
        return web.Response(
            text=(STATIC / "dashboard.html").read_text(encoding="utf-8"), content_type="text/html"
        )

    async def asset(request):
        filename = request.match_info["filename"]
        if filename not in ("dashboard.css", "dashboard.js"):
            raise web.HTTPNotFound()
        return web.Response(
            body=(STATIC / filename).read_bytes(),
            content_type="text/css" if filename.endswith("css") else "application/javascript",
        )

    async def api(request):
        f = Filters(request.query)
        resource = request.match_info["resource"]
        if resource == "overview":
            return web.json_response(await overview(repo, f))
        async with repo.sessions() as session:
            if resource == "users":
                query = select(User)
                if f.user is not None:
                    query = query.where(User.id == f.user)
                if f.provider or f.subscription is not None:
                    query = query.where(
                        User.id.in_(select(Subscription.telegram_user_id).where(*f.subscriptions()))
                    )
                if f.search:
                    pattern = f"%{f.search}%"
                    query = query.where(
                        or_(
                            cast(User.id, String).contains(f.search),
                            User.username.ilike(pattern),
                            User.full_name.ilike(pattern),
                        )
                    )
                if f.status == "blocked":
                    query = query.where(User.blocked.is_(True))
                result = await paginated(session, query, User, f, User.created_at.desc())
                ids = [u["id"] for u in result["items"]]
                counts = dict(
                    (
                        await session.execute(
                            select(Subscription.telegram_user_id, func.count())
                            .where(
                                Subscription.telegram_user_id.in_(ids),
                                Subscription.enabled.is_(True),
                                Subscription.deleted.is_(False),
                                Subscription.date_to >= utcnow().astimezone(KYIV).date(),
                            )
                            .group_by(Subscription.telegram_user_id)
                        )
                    ).all()
                )
                interactions = dict(
                    (
                        await session.execute(
                            select(BotEvent.user_id, func.count())
                            .where(
                                BotEvent.user_id.in_(ids),
                                *f.period(BotEvent.created_at),
                                BotEvent.kind.in_(["command", "callback", "message"]),
                            )
                            .group_by(BotEvent.user_id)
                        )
                    ).all()
                )
                for user in result["items"]:
                    user.update(
                        active_subscriptions=counts.get(user["id"], 0),
                        interactions=interactions.get(user["id"], 0),
                    )
            elif resource == "subscriptions":
                query = select(Subscription).where(*f.subscriptions())
                today = utcnow().astimezone(KYIV).date()
                if f.status == "active":
                    query = query.where(
                        Subscription.enabled.is_(True),
                        Subscription.deleted.is_(False),
                        Subscription.date_to >= today,
                    )
                elif f.status == "paused":
                    query = query.where(
                        Subscription.enabled.is_(False), Subscription.deleted.is_(False)
                    )
                elif f.status == "deleted":
                    query = query.where(Subscription.deleted.is_(True))
                elif f.status == "expired":
                    query = query.where(
                        Subscription.date_to < today, Subscription.deleted.is_(False)
                    )
                if f.search:
                    query = query.where(Subscription.location_name.ilike(f"%{f.search}%"))
                result = await paginated(session, query, Subscription, f, Subscription.id.desc())
                titles = dict(
                    (await session.execute(select(ServiceRecord.id, ServiceRecord.name))).all()
                )
                for sub in result["items"]:
                    sub["service_name"] = titles.get(
                        sub["canonical_service_id"], sub["canonical_service_id"]
                    )
            elif resource in ("runs", "requests"):
                query = (
                    select(PollRun).where(*f.period(PollRun.started_at), *f.run_scope())
                    if resource == "runs"
                    else select(ProviderRequest)
                    .join(PollRun)
                    .where(*f.period(ProviderRequest.started_at), *f.run_scope())
                )
                model = PollRun if resource == "runs" else ProviderRequest
                if f.status:
                    query = query.where(model.status == f.status)
                if request.query.get("run_id"):
                    try:
                        query = query.where(PollRun.id == int(request.query["run_id"]))
                    except ValueError:
                        raise web.HTTPBadRequest(text="Некоректний run_id") from None
                if f.search:
                    query = query.where(PollRun.job_id.contains(f.search))
                result = await paginated(session, query, model, f, model.id.desc())
                if resource == "runs":
                    links = (
                        await session.scalars(
                            select(PollRunSubscription).where(
                                PollRunSubscription.run_id.in_([r["id"] for r in result["items"]])
                            )
                        )
                    ).all()
                    for run in result["items"]:
                        run["subscriptions"] = [
                            serialize(link) for link in links if link.run_id == run["id"]
                        ]
                else:
                    runs = {
                        r.id: r
                        for r in (
                            await session.scalars(
                                select(PollRun).where(
                                    PollRun.id.in_([r["run_id"] for r in result["items"]])
                                )
                            )
                        ).all()
                    }
                    for item in result["items"]:
                        run = runs[item["run_id"]]
                        item.update(
                            job_id=run.job_id,
                            provider_id=run.provider_id,
                            department=run.data.get("department"),
                            service=run.data.get("service"),
                        )
            elif resource == "events":
                query = select(BotEvent).where(*f.events())
                if request.query.get("exclude_outgoing") == "true":
                    query = query.where(BotEvent.kind.in_(["command", "callback", "message"]))
                if f.kind:
                    query = query.where(BotEvent.kind == f.kind)
                if f.status:
                    query = query.where(BotEvent.status == f.status)
                if f.search:
                    query = query.where(BotEvent.text.ilike(f"%{f.search}%"))
                result = await paginated(session, query, BotEvent, f, BotEvent.id.desc())
            elif resource == "timeline":
                events = select(
                    BotEvent.id.label("id"),
                    literal("event").label("origin"),
                    BotEvent.created_at.label("time"),
                    BotEvent.status.label("status"),
                    BotEvent.kind.label("kind"),
                    BotEvent.text.label("text"),
                ).where(*f.events())
                runs = select(
                    PollRun.id,
                    literal("run"),
                    PollRun.started_at,
                    PollRun.status,
                    literal("poll"),
                    PollRun.job_id,
                ).where(*f.period(PollRun.started_at), *f.run_scope())
                merged = union_all(events, runs).subquery()
                query = select(merged)
                if f.status:
                    query = query.where(merged.c.status == f.status)
                if f.kind:
                    query = query.where(merged.c.kind == f.kind)
                if f.search:
                    query = query.where(merged.c.text.ilike(f"%{f.search}%"))
                total = await session.scalar(select(func.count()).select_from(query.subquery()))
                entries = (
                    (
                        await session.execute(
                            query.order_by(
                                merged.c.time.desc(), merged.c.origin, merged.c.id.desc()
                            )
                            .offset((f.page - 1) * f.size)
                            .limit(f.size)
                        )
                    )
                    .mappings()
                    .all()
                )
                event_ids = [e["id"] for e in entries if e["origin"] == "event"]
                run_ids = [e["id"] for e in entries if e["origin"] == "run"]
                event_rows = {
                    e.id: serialize(e)
                    for e in (
                        await session.scalars(select(BotEvent).where(BotEvent.id.in_(event_ids)))
                    ).all()
                }
                run_rows = {
                    r.id: serialize(r)
                    for r in (
                        await session.scalars(select(PollRun).where(PollRun.id.in_(run_ids)))
                    ).all()
                }
                links = (
                    await session.scalars(
                        select(PollRunSubscription).where(PollRunSubscription.run_id.in_(run_ids))
                    )
                ).all()
                for run in run_rows.values():
                    run["subscriptions"] = [
                        serialize(link) for link in links if link.run_id == run["id"]
                    ]
                items = []
                for entry in entries:
                    if entry["origin"] == "event":
                        items.append(event_rows[entry["id"]])
                    else:
                        run = run_rows[entry["id"]]
                        items.append(
                            {
                                **run,
                                "created_at": run["started_at"],
                                "kind": "poll",
                                "user_id": f.user,
                                "text": f"Перевірка {run['job_id']}: {run['slot_count']} слотів",
                            }
                        )
                result = {"items": items, "total": total, "page": f.page, "size": f.size}
            elif resource == "notifications":
                query = select(Notification).where(*f.period(Notification.created_at))
                if f.user is not None:
                    query = query.where(Notification.telegram_user_id == f.user)
                if f.provider:
                    query = query.where(
                        Notification.job_id.in_(
                            select(PollJob.id).where(PollJob.provider_id == f.provider)
                        )
                    )
                if f.subscription is not None:
                    # JSON membership is evaluated with dialect-specific SQL, never substring IDs.
                    if repo.engine.dialect.name == "postgresql":
                        members = func.json_array_elements_text(
                            Notification.data["subscription_ids"]
                        ).table_valued("value")
                    else:
                        members = func.json_each(
                            Notification.data["subscription_ids"]
                        ).table_valued("value")
                    query = query.where(
                        select(members.c.value)
                        .where(cast(members.c.value, String) == str(f.subscription))
                        .exists()
                    )
                if f.status:
                    query = query.where(Notification.status == f.status)
                result = await paginated(session, query, Notification, f, Notification.id.desc())
            elif resource == "jobs":
                query = select(PollJob)
                if f.provider:
                    query = query.where(PollJob.provider_id == f.provider)
                if f.user is not None or f.subscription is not None:
                    query = query.where(
                        PollJob.id.in_(select(PollRun.job_id).where(*f.run_scope()))
                    )
                if f.status:
                    query = query.where(PollJob.status == f.status)
                result = await paginated(session, query, PollJob, f, PollJob.next_run.asc())
            else:
                raise web.HTTPNotFound()
        return web.json_response(result)

    application.router.add_get("/dashboard", index)
    application.router.add_get("/dashboard/", index)
    application.router.add_get("/dashboard/assets/{filename}", asset)
    application.router.add_get("/dashboard/api/{resource}", api)

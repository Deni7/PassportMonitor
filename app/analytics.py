"""Durable audit records without making Telegram delivery depend on audit completion."""

import asyncio
import logging
import time
from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from app.domain import utcnow
from app.models import BotEvent, User

log = logging.getLogger(__name__)
delivery_context = ContextVar("delivery_context", default=None)
incoming_context = ContextVar("incoming_context", default=None)


async def answer_with_diagnostics(message, text, details):
    token = delivery_context.set(
        {**(delivery_context.get() or {}), "provider_error_details": details}
    )
    try:
        return await message.answer(text)
    finally:
        delivery_context.reset(token)


async def record_event(repo, **values):
    try:
        async with repo.sessions.begin() as session:
            row = BotEvent(**values)
            session.add(row)
            await session.flush()
            return row.id
    except Exception as exc:
        log.error("audit_write_failed", extra={"fields": {"type": type(exc).__name__}})
        return None


async def finish_event(repo, event_id, status, started, **details):
    if event_id is None:
        return
    try:
        async with repo.sessions.begin() as session:
            row = await session.get(BotEvent, event_id)
            row.status = status
            row.finished_at = utcnow()
            row.duration_ms = (time.monotonic() - started) * 1000
            row.data = {**row.data, **details}
    except Exception as exc:
        log.error("audit_finish_failed", extra={"fields": {"type": type(exc).__name__}})


class IncomingAudit(BaseMiddleware):
    def __init__(self, repo):
        self.repo = repo

    async def __call__(self, handler, event, data):
        message = event.message
        callback = event.callback_query
        source = callback or message
        chat = (
            callback.message.chat
            if callback and callback.message
            else getattr(message, "chat", None)
        )
        if not source or not chat or chat.type != "private" or not source.from_user:
            return await handler(event, data)
        person = source.from_user
        async with self.repo.sessions.begin() as session:
            user = await session.get(User, person.id)
            if user is None:
                user = User(id=person.id, chat_id=chat.id)
                session.add(user)
            user.username, user.full_name = person.username, person.full_name
            user.language_code, user.last_seen = person.language_code, utcnow()
            user.chat_id, user.blocked = chat.id, False
        state = data.get("state")
        state_before = await state.get_state() if state else None
        text = (callback.data or "") if callback else (message.text or message.caption or "")
        state_data = await state.get_data() if state else {}
        selected = None
        if callback and ":" in text:
            prefix, index = text.split(":", 1)
            if prefix == state_data.get("prefix") and index.isdigit():
                values = state_data.get("choices", [])
                if int(index) < len(values):
                    selected = values[int(index)]
        started = time.monotonic()
        event_id = await record_event(
            self.repo,
            user_id=person.id,
            chat_id=chat.id,
            kind="callback" if callback else "command" if text.startswith("/") else "message",
            status="PROCESSING",
            text=text,
            data={
                "update_id": event.update_id,
                "state_before": state_before,
                "message_id": getattr(message, "message_id", None),
                "selected": selected,
            },
        )
        context = {"event_id": event_id}
        if text.startswith("manage:") and text.rsplit(":", 1)[-1].isdigit():
            context["subscription_id"] = int(text.rsplit(":", 1)[-1])
        context_token = incoming_context.set(context)
        try:
            result = await handler(event, data)
        except BaseException as exc:
            await finish_event(
                self.repo,
                event_id,
                "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "ERROR",
                started,
                error=type(exc).__name__,
            )
            raise
        else:
            from aiogram.dispatcher.event.bases import UNHANDLED

            await finish_event(
                self.repo,
                event_id,
                "ERROR"
                if context.get("error")
                else "UNHANDLED"
                if result is UNHANDLED
                else "HANDLED",
                started,
                state_after=await state.get_state() if state else None,
                error=context.get("error"),
            )
            return result
        finally:
            incoming_context.reset(context_token)


class OutgoingAudit(BaseRequestMiddleware):
    def __init__(self, repo):
        self.repo = repo

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, SendMessage):
            return await make_request(bot, method)
        started = time.monotonic()
        user_id = None
        try:
            async with self.repo.sessions() as session:
                user_id = await session.scalar(
                    select(User.id).where(User.chat_id == method.chat_id)
                )
        except Exception as exc:
            log.error("audit_user_lookup_failed", extra={"fields": {"type": type(exc).__name__}})
        event_id = await record_event(
            self.repo,
            user_id=user_id,
            chat_id=method.chat_id if isinstance(method.chat_id, int) else None,
            kind="outgoing",
            status="SENDING",
            text=method.text,
            data={
                **(delivery_context.get() or {}),
                "source_event_id": (incoming_context.get() or {}).get("event_id"),
                "buttons": method.reply_markup.model_dump(mode="json")
                if method.reply_markup
                else None,
            },
        )
        try:
            result = await make_request(bot, method)
        except BaseException as exc:
            status = (
                "UNCERTAIN"
                if isinstance(exc, (TelegramNetworkError, asyncio.CancelledError))
                else "ERROR"
            )
            await finish_event(self.repo, event_id, status, started, error=type(exc).__name__)
            raise
        else:
            await finish_event(
                self.repo, event_id, "SENT", started, message_id=getattr(result, "message_id", None)
            )
            return result

import logging
from datetime import date
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics import answer_with_diagnostics, incoming_context
from app.domain import ProviderError, utcnow
from app.services.display import PROVIDER_TITLES, readable_errors, status_title

log = logging.getLogger(__name__)


class Wizard(StatesGroup):
    provider = State()
    region = State()
    city = State()
    service = State()
    dates = State()


def keyboard(items):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=callback)] for text, callback in items
        ]
    )


def menu():
    return keyboard([("➕ Додати моніторинг", "add"), ("📋 Мої моніторинги", "list")])


async def choices(message, state, values, prefix, page=0):
    await state.update_data(choices=values, prefix=prefix)
    items = [
        (c["name"], f"{prefix}:{i}")
        for i, c in enumerate(values)
        if page * 12 <= i < (page + 1) * 12
    ]
    if page:
        items.append(("⬅️ Назад", f"page:{page - 1}"))
    if (page + 1) * 12 < len(values):
        items.append(("Далі ➡️", f"page:{page + 1}"))
    items.append(("Скасувати", "cancel"))
    await message.answer("Оберіть зі списку:", reply_markup=keyboard(items))


def build_router(repo, catalog, scheduler):
    router = Router()
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message(Command("start", "cancel"))
    async def start(message, state: FSMContext):
        if message.chat.type != "private":
            await message.answer("Налаштуйте моніторинг у приватному чаті з ботом.")
            return
        await state.clear()
        await repo.user(message.from_user.id, message.chat.id)
        await message.answer(
            "Моніторинг електронних черг. Бот повідомляє про місця; запис здійснюється на офіційному сайті.",
            reply_markup=menu(),
        )

    @router.callback_query(F.data == "cancel")
    async def cancel(callback, state: FSMContext):
        await callback.answer()
        await state.clear()
        await callback.message.answer("Головне меню", reply_markup=menu())

    @router.callback_query(F.data == "add")
    async def add(callback, state: FSMContext):
        await callback.answer()
        await state.clear()
        await repo.user(callback.from_user.id, callback.message.chat.id)
        await state.set_state(Wizard.provider)
        items = []
        if "dmsu" in catalog.providers:
            items.append(("ДМСУ", "provider:dmsu"))
        if "document" in catalog.providers:
            items.append(("ДП «Документ»", "provider:document"))
        if len(items) == 2:
            items.append(("ДМСУ + ДП «Документ»", "provider:dmsu+document"))
        await callback.message.answer("Що моніторимо?", reply_markup=keyboard(items))

    @router.callback_query(Wizard.provider, F.data.startswith("provider:"))
    async def provider(callback, state: FSMContext):
        await callback.answer()
        mode = callback.data.split(":", 1)[1].split("+")
        if any(p not in catalog.providers for p in mode):
            return
        await callback.message.answer("Завантажую актуальний каталог установ…")
        values = await catalog.region_choices(mode)
        await state.set_state(Wizard.region)
        await choices(callback.message, state, values, "region")

    @router.callback_query(
        StateFilter(Wizard.region, Wizard.city, Wizard.service), F.data.startswith("page:")
    )
    async def page(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        number = int(callback.data.split(":")[1])
        if 0 <= number <= (len(data["choices"]) - 1) // 12:
            await choices(callback.message, state, data["choices"], data["prefix"], number)

    @router.callback_query(
        StateFilter(Wizard.region, Wizard.city), F.data.regexp(r"^(region|city):\d+$")
    )
    async def location(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        prefix, index = callback.data.split(":")
        if prefix != data.get("prefix") or int(index) >= len(data.get("choices", [])):
            return
        chosen = data["choices"][int(index)]
        if "region" in chosen:
            await callback.message.answer("Завантажую всі підрозділи області…")
            values = await catalog.city_choices(chosen)
            if chosen.get("provider_errors"):
                await answer_with_diagnostics(
                    callback.message,
                    "⚠️ "
                    + readable_errors(chosen["provider_errors"])
                    + ". Доступна установа продовжить моніторинг; недоступна перевірятиметься після відновлення.",
                    chosen.get("provider_error_details", []),
                )
            await state.set_state(Wizard.city)
            await choices(callback.message, state, values, "city")
            return
        await callback.message.answer(
            "Перевіряю послуги в усіх підрозділах. Це може зайняти кілька хвилин…"
        )
        services = await catalog.available_services(chosen)
        if chosen.get("provider_errors"):
            await answer_with_diagnostics(
                callback.message,
                "⚠️ Каталог частково недоступний: "
                + readable_errors(chosen["provider_errors"])
                + ". Підписка збереже обрані установи, а їх помилки будуть показані окремо.",
                chosen.get("provider_error_details", []),
            )
        if not services:
            await callback.message.answer(
                "У вибраній зоні немає послуг із доступним електронним записом.",
                reply_markup=menu(),
            )
            await state.clear()
            return
        await state.update_data(location=chosen)
        await state.set_state(Wizard.service)
        await choices(
            callback.message,
            state,
            [{"name": name, "id": key} for key, name in sorted(services.items())],
            "service",
        )

    @router.callback_query(Wizard.service, F.data.regexp(r"^service:\d+$"))
    async def service(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        index = int(callback.data.split(":")[1])
        if data.get("prefix") != "service" or index >= len(data["choices"]):
            return
        await state.update_data(service=data["choices"][index])
        await state.set_state(Wizard.dates)
        await callback.message.answer(
            "Введіть діапазон дат: YYYY-MM-DD YYYY-MM-DD (до 90 днів), або «30» для наступних 30 днів."
        )

    @router.message(Wizard.dates, F.text)
    async def dates(message, state: FSMContext):
        from datetime import timedelta

        today = utcnow().astimezone(ZoneInfo("Europe/Kyiv")).date()
        try:
            if message.text.strip() == "30":
                start, end = today, today + timedelta(days=29)
            else:
                parts = message.text.split()
                if len(parts) != 2:
                    raise ValueError()
                start, end = (date.fromisoformat(v) for v in parts)
            if (
                start < today
                or end < start
                or (end - start).days > 89
                or end > today + timedelta(days=365)
            ):
                raise ValueError()
        except ValueError:
            await message.answer(
                "Некоректний діапазон. Початок — не раніше сьогодні, тривалість — до 90 днів. Формат: YYYY-MM-DD YYYY-MM-DD."
            )
            return
        data = await state.get_data()
        sub_id = await repo.add_subscription(
            message.from_user.id, data["location"], data["service"]["id"], start, end
        )
        await state.clear()
        await message.answer(
            f"✅ Моніторинг #{sub_id} створено: {data['location']['name']}, {data['service']['name']}. Перевіряю всі відповідні підрозділи.",
            reply_markup=menu(),
        )

    @router.callback_query(F.data == "list")
    async def subscriptions(callback):
        await callback.answer()
        values = await repo.subscriptions(callback.from_user.id)
        if not values:
            await callback.message.answer("Моніторингів ще немає.", reply_markup=menu())
        for sub in values:
            items = [
                (
                    "⏸ Призупинити" if sub.enabled else "▶️ Відновити",
                    f"manage:{'pause' if sub.enabled else 'resume'}:{sub.id}",
                ),
                ("🔄 Перевірити зараз", f"manage:check:{sub.id}"),
                ("🗑 Видалити", f"manage:delete:{sub.id}"),
            ]
            await callback.message.answer(
                f"#{sub.id} — {sub.location_name}\nУстанови: {', '.join(PROVIDER_TITLES[p] for p in sub.provider_mode.split('+'))}\nПослуга: {await repo.service_title(sub.canonical_service_id)}\nДати: {sub.date_from} — {sub.date_to}\n{'Активний' if sub.enabled else 'Призупинено'}",
                reply_markup=keyboard(items),
            )

    @router.callback_query(F.data.regexp(r"^manage:(pause|resume|delete|check):\d+$"))
    async def manage(callback):
        await callback.answer()
        _, action, raw_id = callback.data.split(":")
        sub_id = int(raw_id)
        if action == "check":
            subs = await repo.subscriptions(callback.from_user.id)
            sub = next((s for s in subs if s.id == sub_id), None)
            if sub:
                messages = await scheduler.cached_status(sub)
                for offset in range(0, len(messages), 10):
                    await callback.message.answer(
                        "Кеш останніх перевірок:\n" + "\n".join(messages[offset : offset + 10])
                    )
        elif await repo.manage(callback.from_user.id, sub_id, action):
            await callback.message.answer("✅ Збережено", reply_markup=menu())

    @router.errors()
    async def error(event):
        context = incoming_context.get()
        if context is not None:
            context["error"] = type(event.exception).__name__
        if isinstance(event.exception, ProviderError):
            text = f"⚠️ Провайдер тимчасово недоступний: {status_title(event.exception.status)}. Спробуйте /start пізніше. Це не означає відсутність місць."
        else:
            log.error(
                "bot_handler_failed", extra={"fields": {"type": type(event.exception).__name__}}
            )
            text = "⚠️ Помилка обробки. Спробуйте /start."
        update = event.update
        message = update.message or (
            update.callback_query.message if update.callback_query else None
        )
        if message:
            if isinstance(event.exception, ProviderError):
                await answer_with_diagnostics(message, text, [event.exception.as_dict()])
            else:
                await message.answer(text)
        return True

    return router

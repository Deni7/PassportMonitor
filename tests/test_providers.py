import json
from datetime import date
from pathlib import Path

import pytest

from app.domain import Department, ProviderError, Service
from app.providers.dmsu import parse_department, rows
from app.providers.document import (
    DocumentProvider,
    available_count,
    parse_centers,
    parse_days,
    parse_services,
    parse_times,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_all_365_real_departments_have_city_and_address():
    payload = json.loads((FIXTURES / "dmsu/all-departments.json").read_text(encoding="utf-8-sig"))
    departments = [
        parse_department(r, region)
        for region, data in payload.items()
        for r in rows(data, ("id", "label"))
    ]
    assert len(payload) == 22 and len(departments) == 365
    assert all(d.city and d.address for d in departments)
    assert sum(d.city == "Львів" for d in departments) == 3
    assert sum(d.city == "Київ" for d in departments) >= 10


def test_real_dmsu_catalog_contract():
    for name in ("regions", "departments", "services"):
        data = json.loads((FIXTURES / "dmsu" / f"{name}.json").read_text(encoding="utf-8-sig"))
        assert rows(data, ("id", "label"))
    departments = rows(
        json.loads((FIXTURES / "dmsu/departments.json").read_text(encoding="utf-8-sig")),
        ("id", "label"),
    )
    assert len([r for r in departments if parse_department(r, "23").city == "Львів"]) == 3


@pytest.mark.parametrize("data", [{}, {"data": None}, {"data": {}}, {"data": [{"id": 1}]}])
def test_changed_contract_fails(data):
    with pytest.raises(ProviderError, match="contract changed"):
        rows(data, ("id", "label"))


def test_empty_dates_are_success_only_with_correct_envelope():
    assert parse_days({"days": []}) == []
    with pytest.raises(ProviderError):
        parse_days({"error": "blocked"})


def test_document_days_respect_frontend_is_allowed_flag():
    assert parse_days(
        {
            "days": [
                {"datePart": "2026-10-07", "isAllowed": False},
                {"datePart": "2026-10-08", "isAllowed": True},
            ]
        }
    ) == [date(2026, 10, 8)]
    with pytest.raises(ProviderError):
        parse_days({"days": [{"datePart": "2026-10-07", "isAllowed": "true"}]})


def test_document_contract_from_frontend_excludes_unavailable_times():
    # Synthetic branch coverage based on discovered frontend, not a live API capture.
    dep = Department(
        "20", "Львів", "Адреса", "Львів", "львів", "https://lviv2.pasport.org.ua/solutions/e-queue"
    )
    data = {
        "timeSlots": [
            {"startTime": "09:30:00", "isAllowed": True},
            {"startTime": "09:50:00", "isAllowed": False},
        ]
    }
    assert [s.time for s in parse_times(data, dep, Service("4", "Паспорт"), date(2026, 10, 7))] == [
        "09:30"
    ]
    with pytest.raises(ProviderError):
        parse_times({"timeSlots": [{"isAllowed": "true"}]}, dep, Service("4", ""), date.today())


def test_document_real_public_dom_catalog():
    html = (FIXTURES / "document/public_catalog.html").read_text(encoding="utf-8")
    assert parse_centers(html)[0].city == "Львів"
    assert parse_services(html)[0].id == "4"


def test_document_live_positive_json_contract():
    captured = FIXTURES / "document/live"
    days = parse_days(json.loads((captured / "05-days.json").read_text()))
    assert days == [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)]
    dep = Department(
        "gotovo.pasport.org.ua",
        "Готово",
        "",
        "Київ",
        "київ",
        "https://gotovo.pasport.org.ua/solutions/e-queue",
    )
    slots = parse_times(
        json.loads((captured / "06-timeSlots.json").read_text(encoding="utf-8")),
        dep,
        Service("2", "Обмін/відновлення водійського посвідчення."),
        days[0],
    )
    assert len(slots) == 6
    assert slots[0].time == "09:00"
    assert all(s.count == 14 for s in slots)
    assert available_count("09:00 — 1 вільний слот") == 1
    assert available_count("09:00") is None


async def test_document_retry_after_is_preserved_without_immediate_retry():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import Settings
    from app.domain import Status

    provider = DocumentProvider(
        Settings(database_url="sqlite+aiosqlite:///:memory:", telegram_bot_token="123456:test"),
        None,
    )
    provider._respect_retry_after(SimpleNamespace(headers={"retry-after": "900"}))
    deadline = provider.blocked_until
    provider.blocked_until = 0  # Simulate the in-flight request reporting the block.

    async def blocked():
        provider.blocked_until = deadline
        raise ProviderError(Status.RATE_LIMIT, "HTTP 429")

    with pytest.raises(ProviderError):
        await provider._operation(blocked)
    assert provider.blocked_until >= deadline
    action = AsyncMock()
    with pytest.raises(ProviderError):
        await provider._operation(action)
    action.assert_not_awaited()

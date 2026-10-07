from datetime import timedelta

import pytest

from app.config import Settings
from app.domain import utcnow
from app.providers.dmsu import DMSUProvider
from app.providers.document import DocumentProvider
from app.providers.transport import Gate, Transport

pytestmark = pytest.mark.integration


def config():
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        telegram_bot_token="123456:integration-read-only",
    )


async def test_live_dmsu_readonly_contract():
    settings = config()
    gate = Gate(settings.global_request_interval, settings.provider_request_interval)
    provider = DMSUProvider(Transport("dmsu", settings, gate))
    try:
        locations = await provider.get_locations()
        location = next(r for r in locations if r.name == "Львівська область")
        departments = await provider.get_departments(location)
        dep = next(d for d in departments if d.city == "Львів")
        services = await provider.get_services(dep)
        days = await provider.get_available_dates(
            dep, services[0], utcnow().date(), utcnow().date() + timedelta(days=30)
        )
        if days:
            await provider.get_available_slots(dep, services[0], days[0])
    finally:
        await provider.close()


async def test_live_document_readonly_contract():
    settings = config()
    provider = DocumentProvider(
        settings, Gate(settings.global_request_interval, settings.provider_request_interval)
    )
    try:
        locations = await provider.get_locations()
        location = next(r for r in locations if r.city == "Львів")
        for dep in await provider.get_departments(location):
            services = await provider.get_services(dep)
            days = await provider.get_available_dates(
                dep, services[0], utcnow().date(), utcnow().date() + timedelta(days=30)
            )
            if days:
                await provider.get_available_slots(dep, services[0], days[0])
    finally:
        await provider.close()

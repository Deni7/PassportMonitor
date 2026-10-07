from dataclasses import asdict
from unittest.mock import AsyncMock

from app.domain import Department, Location, ProviderError, Service, Status
from app.services.catalog import Catalog
from app.services.mapping import DMSU_COMBINED, service_matches


async def test_all_matching_departments_including_combined_provider_city(repo):
    region = Location("23", "Львівська область", "23")
    departments = [
        Department("101", "A", "Адреса A", "Львів", "23", "https://cherga.dmsu.gov.ua/"),
        Department("102", "B", "Адреса B", "Львів", "23", "https://cherga.dmsu.gov.ua/"),
        Department("97", "C", "Адреса C", "Турка", "23", "https://cherga.dmsu.gov.ua/"),
    ]
    dmsu = AsyncMock()
    dmsu.get_departments.return_value = departments
    dmsu.get_services.return_value = [
        Service("1", "Оформлення паспорта громадянина України для виїзду за кордон")
    ]
    document = AsyncMock()
    document.get_locations.return_value = [
        Location("львів", "Львів", city="Львів"),
        Location("миколаїв", "Миколаїв", city="Миколаїв"),
    ]
    document.get_departments.return_value = [
        Department(
            "lviv2.pasport.org.ua",
            "D",
            "Адреса D",
            "Львів",
            "львів",
            "https://lviv2.pasport.org.ua/solutions/e-queue",
        )
    ]
    document.get_services.return_value = [Service("4", "Закордонний паспорт та (або) ID-картка")]
    catalog = Catalog({"dmsu": dmsu, "document": document}, repo, 600)
    choices = await catalog.city_choices({"region": asdict(region), "mode": ["dmsu", "document"]})
    lviv = next(c for c in choices if c["name"] == "Львів")
    assert len(await catalog.departments("dmsu", Location(**lviv["locations"]["dmsu"][0]))) == 2
    assert "document" in lviv["locations"]
    assert "passport" in await catalog.available_services(lviv)
    assert dmsu.get_departments.await_count == 1  # shared region catalog across city subscriptions
    assert all(c["name"] != "Миколаїв" for c in choices)


def test_different_provider_service_titles_map_to_one_logical_service():
    assert service_matches(
        "dmsu",
        Service("1", "Оформлення паспорта громадянина України для виїзду за кордон"),
        "passport",
    )


async def test_document_outage_keeps_both_provider_subscription_scope(repo):
    region = Location("23", "Львівська область", "23")
    dmsu, document = AsyncMock(), AsyncMock()
    dmsu.get_departments.return_value = [
        Department("101", "A", "Адреса", "Львів", "23", "https://cherga.dmsu.gov.ua/")
    ]
    dmsu.get_services.return_value = [
        Service("1", "Оформлення паспорта громадянина України для виїзду за кордон")
    ]
    document.get_locations.side_effect = ProviderError(Status.CLOUDFLARE, "challenge")
    document.get_departments.side_effect = ProviderError(Status.CLOUDFLARE, "challenge")
    catalog = Catalog({"dmsu": dmsu, "document": document}, repo, 600)
    choices = await catalog.city_choices({"region": asdict(region), "mode": ["dmsu", "document"]})
    choice = next(c for c in choices if c["name"] == "Львів")
    assert set(choice["locations"]) == {"dmsu", "document"}
    assert "passport" in await catalog.available_services(choice)
    assert choice["provider_errors"] == ["document: CLOUDFLARE"]
    assert service_matches(
        "document", Service("4", "Закордонний паспорт та (або) ID-картка"), "passport"
    )


async def test_dmsu_combined_queue_does_not_add_duplicate_menu_choice(repo):
    dmsu = AsyncMock()
    dmsu.get_departments.return_value = [
        Department("101", "Відділ", "Адреса", "Львів", "23", "https://cherga.dmsu.gov.ua/")
    ]
    combined = Service("unseen-id", DMSU_COMBINED)
    dmsu.get_services.return_value = [
        combined,
        Service("1", "Оформлення паспорта громадянина України для виїзду за кордон"),
        Service("2", "Оформлення паспорта громадянина України у вигляді ID картки"),
    ]
    catalog = Catalog({"dmsu": dmsu}, repo, 600)
    choice = {"locations": {"dmsu": [asdict(Location("23", "Львів", "23", "Львів"))]}}
    assert await catalog.available_services(choice) == {
        "passport": "Оформлення закордонного паспорта",
        "id_card": "Оформлення ID-картки",
    }
    assert service_matches("dmsu", combined, "passport")
    assert service_matches("dmsu", combined, "id_card")

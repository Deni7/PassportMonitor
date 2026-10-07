from datetime import date

import pytest

from app.domain import ProviderError, Service, Slot, official_url
from app.services.mapping import canonical_services, service_matches


def test_slot_time_preserves_seconds_and_explicit_timezone():
    from app.domain import appointment_time

    day = date(2026, 10, 6)
    assert appointment_time(day, "09:30:20") == (day, "09:30:20")
    assert appointment_time(day, "23:30:00+00:00") == (date(2026, 10, 7), "02:30")
    assert appointment_time(day, "09:30:00") == (day, "09:30")


def test_fingerprint_excludes_mutable_capacity():
    a = Slot("dmsu", "101", "1", date(2026, 10, 7), "09:00", 1)
    b = Slot("dmsu", "101", "1", date(2026, 10, 7), "09:00", 2)
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != Slot("dmsu", "102", "1", a.day, a.time).fingerprint


@pytest.mark.parametrize(
    "url",
    [
        "https://pasport.org.ua.evil.test/",
        "http://pasport.org.ua/",
        "https://evilpasport.org.ua/",
        "https://user@pasport.org.ua/",
    ],
)
def test_official_url_rejects_untrusted_hosts(url):
    with pytest.raises(ProviderError):
        official_url("document", url)


def test_mapping_does_not_guess_unseen_service_ids():
    combined = Service("999", "Закордонний паспорт та (або) ID-картка")
    assert canonical_services("document", combined) == ["passport", "id_card"]
    assert service_matches("document", combined, "passport")
    assert canonical_services("document", Service("4", "Інша послуга"))[0].startswith("document:")

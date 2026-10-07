import re
from datetime import date

from app.domain import (
    Department,
    Location,
    ProviderError,
    Service,
    Slot,
    Status,
    appointment_time,
    normalize,
)
from app.providers.base import QueueProvider

BASE = "https://cherga.dmsu.gov.ua"


def rows(payload, fields):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: data must be a list")
    result = payload["data"]
    for row in result:
        if not isinstance(row, dict) or any(key not in row for key in fields):
            raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: missing fields")
        if any(isinstance(row[k], bool) or not isinstance(row[k], (str, int)) for k in fields):
            raise ProviderError(
                Status.PARSING_ERROR, "Provider contract changed: invalid field type"
            )
        if any(k != "id" and not isinstance(row[k], str) for k in fields):
            raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: expected string")
        if "id" in fields and not str(row["id"]).isdigit():
            raise ProviderError(
                Status.PARSING_ERROR, "Provider contract changed: expected numeric id"
            )
    return result


def parse_department(row, region):
    label = re.sub(r"\s+", " ", str(row["label"])).strip()
    body = re.sub(r"^\d+(?:\.\d+)?[.,]?\s*", "", label)
    markers = list(re.finditer(r"(?<!\w)(?:м\s*\.|місто|селище\.?|с-ще\.?|смт[.,]?|с\.)\s*", body))
    street = r"(?:вул(?:иця|\.)?|просп(?:ект|\.)?|пр-т\.?|пр-кт\.?|пров(?:улок|\.)|бульвар|бул\.|площа|пл\.|майдан)"
    # Two irregular source labels do not use a separable city/address pattern.
    if str(row["id"]) == "429" and "Сумський відділ № 1" in body:
        city, address = "Суми", body[body.index("вул.") :]
    elif str(row["id"]) == "12" and body == "м. Одеса Десантний бульвар, 16":
        city, address = "Одеса", "Десантний бульвар, 16"
    elif markers:
        marker = markers[-1]
        tail = body[marker.end() :]
        end = re.search(r",|\s+" + street + r"|\s+ГУ ДМС", tail, re.IGNORECASE)
        city = tail[: end.start() if end else len(tail)].strip()
        city = re.sub(r"\s*\([^)]*\)", "", city).strip()
        if city == "Харкові":
            city = "Харків"
        before = body[: marker.start()].strip(" ,")
        address_start = re.search(street, before, re.IGNORECASE)
        address = (
            before[address_start.start() :]
            if address_start
            else tail[len(tail[: end.start() if end else len(tail)]) :].strip(" ,")
        )
        if address.startswith("ГУ ДМС"):
            actual = re.search(street, address, re.IGNORECASE)
            address = address[actual.start() :] if actual else ""
    else:
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: department city")
    if not city or not address:
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: department address")
    return Department(str(row["id"]), label, address.strip(" ,"), city, region, BASE + "/")


class DMSUProvider(QueueProvider):
    name = "dmsu"

    def __init__(self, transport):
        self.http = transport

    async def get_locations(self):
        data = await self.http.get_json(BASE + "/api/v1/departments/regions")
        return [
            Location(str(r["id"]), str(r["label"]), str(r["id"]))
            for r in rows(data, ("id", "label"))
        ]

    async def get_departments(self, location):
        data = await self.http.get_json(BASE + "/api/v1/departments/" + location.region_id)
        departments = [parse_department(r, location.region_id) for r in rows(data, ("id", "label"))]
        return [
            d
            for d in departments
            if not location.city or normalize(d.city) == normalize(location.city)
        ]

    async def get_services(self, department):
        data = await self.http.get_json(BASE + "/api/v1/services/" + department.id)
        return [Service(str(r["id"]), str(r["label"])) for r in rows(data, ("id", "label"))]

    async def get_available_dates(self, department, service, start, end):
        data = await self.http.get_json(
            f"{BASE}/api/v1/days/{department.id}/{service.id}",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        try:
            return sorted(
                {
                    date.fromisoformat(r["date"])
                    for r in rows(data, ("date",))
                    if start <= date.fromisoformat(r["date"]) <= end
                }
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: date") from exc

    async def get_available_slots(self, department, service, day):
        data = await self.http.get_json(
            f"{BASE}/api/v1/time/{department.id}/{service.id}", {"date": day.isoformat()}
        )
        try:
            return [
                Slot(
                    self.name,
                    department.id,
                    service.id,
                    *appointment_time(day, r["time"]),
                )
                for r in rows(data, ("time",))
            ]
        except (TypeError, ValueError) as exc:
            raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: time") from exc

    async def close(self):
        await self.http.close()

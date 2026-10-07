import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def utcnow():
    return datetime.now(UTC)


def appointment_time(day, value):
    parsed = time.fromisoformat(value)
    if parsed.tzinfo:
        local = datetime.combine(day, parsed).astimezone(ZoneInfo("Europe/Kyiv"))
        day, parsed = local.date(), local.time()
    precision = "microseconds" if parsed.microsecond else "seconds" if parsed.second else "minutes"
    return day, parsed.isoformat(timespec=precision)


class Status(StrEnum):
    AVAILABLE = "AVAILABLE"
    NO_SLOTS = "NO_SLOTS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PARSING_ERROR = "PARSING_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    CLOUDFLARE = "CLOUDFLARE"
    CAPTCHA = "CAPTCHA"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    TIMEOUT = "TIMEOUT"
    BROWSER_ERROR = "BROWSER_ERROR"


class ProviderError(Exception):
    def __init__(self, status: Status, detail: str, **diagnostics):
        super().__init__(detail)
        self.status = status
        self.diagnostics = diagnostics

    def as_dict(self):
        return {"status": str(self.status), "detail": str(self), **self.diagnostics}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("’", "'").replace("ʼ", "'")).strip().casefold()


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    region_id: str = ""
    city: str = ""


@dataclass(frozen=True)
class Department:
    id: str
    name: str
    address: str
    city: str
    location_id: str
    booking_url: str


@dataclass(frozen=True)
class Service:
    id: str
    name: str


@dataclass(frozen=True)
class Slot:
    provider: str
    department_id: str
    service_id: str
    day: date
    time: str | None = None
    count: int | None = None

    @property
    def fingerprint(self):
        value = [
            self.provider,
            self.department_id,
            self.service_id,
            self.day.isoformat(),
            self.time,
        ]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def official_url(provider: str, url: str) -> str:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(Status.PARSING_ERROR, "Invalid booking URL") from exc
    host = parsed.hostname or ""
    valid = (
        host == "cherga.dmsu.gov.ua"
        if provider == "dmsu"
        else (host == "pasport.org.ua" or host.endswith(".pasport.org.ua"))
    )
    if parsed.scheme != "https" or not valid or parsed.username or port not in (None, 443):
        raise ProviderError(Status.PARSING_ERROR, "Unofficial booking URL")
    return url

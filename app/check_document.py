"""Read-only live probe. Does not connect to Telegram, book or authenticate."""

import argparse
import asyncio
import json
from datetime import timedelta
from pathlib import Path

from app.config import Settings
from app.domain import ProviderError, normalize, utcnow
from app.providers.document import DocumentProvider
from app.providers.transport import Gate


class RecordingProvider(DocumentProvider):
    def __init__(self, settings, gate, capture):
        super().__init__(settings, gate)
        self.capture = capture
        self.sequence = 0

    async def _response(self, page, selector, value):
        payload = await super()._response(page, selector, value)
        if self.capture:
            self.capture.mkdir(parents=True, exist_ok=True)
            self.sequence += 1
            kind = "days" if selector == "#service" else "timeSlots"
            fields = (
                ("datePart", "date", "allowedJobCount", "isAllowed")
                if kind == "days"
                else ("startTime", "slot", "isAllowed")
            )
            # Only public availability fields. Never save HTML, CSRF, cookies or headers.
            rows = payload.get(kind) if isinstance(payload, dict) else None
            if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                public = {kind: [{k: row[k] for k in fields if k in row} for row in rows]}
                (self.capture / f"{self.sequence:02d}-{kind}.json").write_text(
                    json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        return payload


async def probe(cities, capture):
    settings = Settings(
        telegram_bot_token="123456:readonly-probe", database_url="sqlite+aiosqlite:///:memory:"
    )
    provider = RecordingProvider(
        settings,
        Gate(settings.global_request_interval, settings.provider_request_interval),
        capture,
    )
    try:
        locations = await provider.get_locations()
        print(json.dumps({"catalog_cities": len(locations)}, ensure_ascii=False), flush=True)
        failures = 0
        for city in cities:
            location = next((r for r in locations if normalize(r.city) == normalize(city)), None)
            if location is None:
                print(json.dumps({"city": city, "status": "CITY_NOT_FOUND"}, ensure_ascii=False))
                failures += 1
                continue
            for dep in await provider.get_departments(location):
                try:
                    services = await provider.get_services(dep)
                    for service in services:
                        days = await provider.get_available_dates(
                            dep, service, utcnow().date(), utcnow().date() + timedelta(days=30)
                        )
                        slots = []
                        if days:
                            slots = await provider.get_available_slots(dep, service, days[0])
                        print(
                            json.dumps(
                                {
                                    "department": dep.id,
                                    "city": dep.city,
                                    "service": service.name,
                                    "status": "AVAILABLE" if slots else "NO_SLOTS",
                                    "days": [d.isoformat() for d in days],
                                    "checked_day_slots": len(slots),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                except ProviderError as exc:
                    failures += 1
                    print(json.dumps({"department": dep.id, "status": exc.status}), flush=True)
        return 1 if failures else 0
    except ProviderError as exc:
        print(json.dumps({"status": exc.status}), flush=True)
        return 1
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", action="append", help="Repeat for multiple cities")
    parser.add_argument("--capture-directory", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(probe(args.city or ["Львів", "Київ"], args.capture_directory)))


if __name__ == "__main__":
    main()

import asyncio
import time
from dataclasses import asdict

from app.domain import Location, ProviderError, Status, normalize
from app.services.geography import DOCUMENT_REGIONS
from app.services.mapping import TITLES, canonical_services


class Catalog:
    def __init__(self, providers, repository, ttl):
        self.providers, self.repo, self.ttl = providers, repository, ttl
        self.cache = {}
        self.lock = asyncio.Lock()

    async def _cached(self, key, action):
        async with self.lock:
            item = self.cache.get(key)
            if item and item[0] > time.monotonic():
                if isinstance(item[1], ProviderError):
                    error = item[1]
                    error.diagnostics["cached"] = True
                    raise error
                return item[1]
            try:
                value = await action()
            except ProviderError as exc:
                exc.diagnostics.update(provider=key[0], operation=key[1])
                self.cache[key] = (time.monotonic() + 60, exc)
                raise
            self.cache[key] = (time.monotonic() + self.ttl, value)
            return value

    async def locations(self, provider):
        async def load():
            result = await self.providers[provider].get_locations()
            await self.repo.upsert_catalog(provider, locations=result)
            return result

        return await self._cached((provider, "locations"), load)

    async def departments(self, provider, location):
        # Cache region-wide results, then filter exact city; overlapping subscriptions share requests.
        source = (
            Location(location.region_id, location.name, location.region_id)
            if provider == "dmsu"
            else location
        )

        async def load():
            result = await self.providers[provider].get_departments(source)
            await self.repo.upsert_catalog(provider, departments=result)
            return result

        result = await self._cached((provider, "departments", source.id), load)
        return [
            d for d in result if not location.city or normalize(d.city) == normalize(location.city)
        ]

    async def services(self, provider, department):
        async def load():
            result = await self.providers[provider].get_services(department)
            mapping = [
                (department, s, c, TITLES.get(c, s.name))
                for s in result
                for c in canonical_services(provider, s)
            ]
            await self.repo.upsert_catalog(provider, services=mapping)
            return result

        return await self._cached((provider, "services", department.id), load)

    async def region_choices(self, mode):
        if "dmsu" in mode:
            return [
                {"name": r.name, "key": r.id, "region": asdict(r), "mode": mode}
                for r in await self.locations("dmsu")
            ]
        return [
            {"name": r.name, "key": r.id, "locations": {"document": [asdict(r)]}}
            for r in await self.locations("document")
        ]

    async def city_choices(self, region_choice):
        region = Location(**region_choice["region"])
        departments = await self.departments("dmsu", region)
        cities = {normalize(d.city): d.city for d in departments}
        doc_locations = []
        if "document" in region_choice["mode"]:
            try:
                doc_locations = await self.locations("document")
            except ProviderError as exc:
                region_choice["provider_errors"] = [f"document: {exc.status}"]
                region_choice["provider_error_details"] = [
                    {"provider": "document", "operation": "locations", **exc.as_dict()}
                ]
            for location in doc_locations:
                expected = DOCUMENT_REGIONS.get(location.city)
                if expected is None:
                    raise ProviderError(
                        Status.PARSING_ERROR,
                        "New Document city needs administrative region mapping",
                    )
                if expected == region.name:
                    cities[normalize(location.city)] = location.city
        choices = []
        for key, city in sorted(cities.items()):
            specs = {"dmsu": [asdict(Location(region.id + ":" + key, city, region.id, city))]}
            if "document" in region_choice["mode"]:
                # A geographic filter, not a guessed backend ID. The provider discovers all
                # real departments when available, including after a temporary outage.
                specs["document"] = [
                    asdict(Location("city:" + region.id + ":" + key, city, region.name, city))
                ]
            choices.append({"name": city, "key": region.id + ":" + key, "locations": specs})
        all_specs = {"dmsu": [asdict(region)]}
        if "document" in region_choice["mode"]:
            all_specs["document"] = [
                asdict(Location("region:" + region.id, region.name, region.name))
            ]
        choices.insert(
            0,
            {
                "name": "Уся область — " + region.name,
                "key": "region:" + region.id,
                "locations": all_specs,
            },
        )
        return choices

    async def available_services(self, choice):
        result = {}
        errors = []
        details = []
        for provider, specs in choice["locations"].items():
            for spec in specs:
                try:
                    departments = await self.departments(provider, Location(**spec))
                except ProviderError as exc:
                    errors.append((provider, exc))
                    details.append(
                        {
                            "provider": provider,
                            "operation": "departments",
                            "location_id": spec["id"],
                            **exc.as_dict(),
                        }
                    )
                    continue
                for dep in departments:
                    try:
                        for service in await self.services(provider, dep):
                            for canonical in canonical_services(provider, service):
                                result[canonical] = TITLES.get(canonical, service.name)
                    except ProviderError as exc:
                        errors.append((provider, exc))
                        details.append(
                            {
                                "provider": provider,
                                "operation": "services",
                                "department_id": dep.id,
                                **exc.as_dict(),
                            }
                        )
        choice["provider_errors"] = sorted({f"{p}: {e.status}" for p, e in errors})
        choice["provider_error_details"] = details
        if not result and errors:
            raise errors[0][1]
        return result

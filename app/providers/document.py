import asyncio
import json
import re
import time as clock
from contextlib import suppress
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from app.domain import (
    Department,
    Location,
    ProviderError,
    Service,
    Slot,
    Status,
    appointment_time,
    normalize,
    official_url,
)
from app.observability import DURATION, ERRORS, REQUESTS
from app.providers.base import QueueProvider
from app.providers.transport import backoff, retry_after_seconds
from app.services.geography import DOCUMENT_REGIONS

ENTRY = "https://pasport.org.ua/solutions/e-queue"


def parse_centers(html):
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("[x-data]"):
        value = element.get("x-data", "")
        if not value.startswith("{items:"):
            continue
        try:
            items = json.loads(value[len("{items:") : -1].strip())
            if not isinstance(items, list) or not items:
                raise ValueError()
            result = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError()
                if not all(
                    isinstance(item.get(k), str) and item[k] for k in ("link", "city", "title")
                ):
                    raise ValueError()
                url = official_url("document", item["link"]).rstrip("/")
                result.append(
                    Department(
                        urlparse(url).hostname,
                        "Паспортний сервіс — " + item["city"],
                        item["title"],
                        item["city"],
                        normalize(item["city"]),
                        url + "/solutions/e-queue",
                    )
                )
            # First items block is the observed Ukrainian country block.
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: centers") from exc
    raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: centers JSON absent")


def parse_services(html):
    soup = BeautifulSoup(html, "html.parser")
    form, select = soup.select_one("form#services"), soup.select_one("select#service")
    if not form or not select or "qlogickFormHaku(" not in form.get("x-data", ""):
        raise ProviderError(Status.AUTH_REQUIRED, "Read-only service form unavailable or changed")
    result = [
        Service(o["value"], o.get_text(strip=True))
        for o in select.select("option[value]")
        if o["value"]
    ]
    if not result:
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: services empty")
    return result


def parse_days(payload):
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
            raise ValueError()
        result = []
        for row in payload["days"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("datePart"), str)
                or type(row.get("isAllowed")) is not bool
            ):
                raise ValueError()
            day = date.fromisoformat(row["datePart"])
            if row["isAllowed"]:
                result.append(day)
        return sorted(set(result))
    except (ValueError, TypeError) as exc:
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: days") from exc


def parse_times(payload, department, service, day):
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("timeSlots"), list):
            raise ValueError()
        result = []
        for row in payload["timeSlots"]:
            if not isinstance(row, dict) or type(row.get("isAllowed")) is not bool:
                raise ValueError()
            if row["isAllowed"]:
                result.append(
                    Slot(
                        "document",
                        department.id,
                        service.id,
                        *appointment_time(day, row["startTime"]),
                        count=available_count(row.get("slot")),
                    )
                )
        return result
    except (ValueError, TypeError, KeyError) as exc:
        raise ProviderError(Status.PARSING_ERROR, "Provider contract changed: timeSlots") from exc


def available_count(label):
    # The live Ukrainian frontend exposes capacity in the public slot label.
    match = (
        re.search(r"\b(\d+)\s+вільн(?:их|ий|і)\s+слот(?:ів|и)?\b", label or "")
        if isinstance(label, str)
        else None
    )
    return int(match[1]) if match else None


class DocumentProvider(QueueProvider):
    name = "document"
    date_window_days = None

    def __init__(self, settings, gate):
        self.settings, self.gate = settings, gate
        self.runtime = self.browser = self.context = None
        self.pages = {}
        self.lock = asyncio.Lock()
        self.blocked_until = 0.0
        self.failures = 0
        self.centers = None
        self.centers_fetched = 0.0

    async def _start(self):
        if self.browser is not None and not self.browser.is_connected():
            await self.close()
        if self.runtime is None:
            self.runtime = await async_playwright().start()
        if self.browser is None:
            self.browser = await self.runtime.chromium.launch(
                headless=self.settings.document_browser_headless
            )
        if self.context is None:
            self.context = await self.browser.new_context(timezone_id="Europe/Kyiv")
            context = self.context
            context.on("close", lambda: self._context_closed(context))
            await self.context.route("**/*", self._route)

    def _context_closed(self, context):
        if self.context is context:
            self.context = None
            self.pages.clear()

    async def _route(self, route):
        req = route.request
        host = urlparse(req.url).hostname or ""
        if host != "pasport.org.ua" and not host.endswith(".pasport.org.ua"):
            # Avoid analytics, external fingerprint collectors and authentication services.
            await route.abort()
            return
        if req.method != "GET":
            body = req.post_data or ""
            readonly = bool(re.search(r'name="form"\s*\r?\n\r?\n(?:days|times)\b', body))
            if req.method != "POST" or not readonly:
                await route.abort()
                return
        if req.resource_type in ("document", "xhr", "fetch"):
            await self.gate.wait(self.name)
            REQUESTS.labels(self.name).inc()
        await route.continue_()

    async def _challenge(self, page):
        title = (await page.title()).lower()
        if (
            "just a moment" in title
            or await page.locator("#challenge-running,#challenge-form").count()
        ):
            raise ProviderError(Status.CLOUDFLARE, "Browser challenge; monitoring suspended")
        if await page.locator('iframe[src*="hcaptcha"],iframe[src*="recaptcha"]').count():
            raise ProviderError(Status.CAPTCHA, "CAPTCHA; manual official registration required")

    async def _page(self, url, refresh=False):
        official_url(self.name, url)
        await self._start()
        # Bounded page cache: one catalog page and one working page, one browser process.
        key = "catalog" if url == ENTRY else "working"
        page = self.pages.get(key)
        if page is None or page.is_closed():
            page = await self.context.new_page()
            page.set_default_timeout(self.settings.http_timeout * 1000)
            self.pages[key] = page
        if page.url != url or refresh:
            response = await page.goto(url, wait_until="domcontentloaded")
            if response and response.status in (403, 429, 503):
                self._respect_retry_after(response)
            await self._challenge(page)
            if response and response.status in (403, 429, 503):
                raise ProviderError(Status.RATE_LIMIT, f"Browser HTTP {response.status}")
        await self._challenge(page)
        return page

    def _respect_retry_after(self, response):
        delay = retry_after_seconds(
            response.headers.get("retry-after", "0"), self.settings.circuit_cooldown
        )
        self.blocked_until = max(
            self.blocked_until, clock.monotonic() + max(delay, self.settings.circuit_cooldown)
        )

    async def _operation(self, action):
        for attempt in range(self.settings.max_retries + 1):
            try:
                return await self._attempt_operation(action)
            except ProviderError as exc:
                if (
                    exc.status not in (Status.TIMEOUT, Status.BROWSER_ERROR)
                    or attempt == self.settings.max_retries
                    or clock.monotonic() < self.blocked_until
                ):
                    raise
                await asyncio.sleep(backoff(attempt, self.settings.backoff_factor))

    async def _attempt_operation(self, action):
        async with self.lock:
            if clock.monotonic() < self.blocked_until:
                raise ProviderError(Status.RATE_LIMIT, "Browser circuit open")
            started = clock.monotonic()
            try:
                result = await action()
                self.failures = 0
                return result
            except BrowserTimeout as exc:
                error = ProviderError(Status.TIMEOUT, "Browser availability request timed out")
                error.__cause__ = exc
            except BrowserError as exc:
                error = ProviderError(Status.BROWSER_ERROR, "Playwright operation failed")
                error.__cause__ = exc
            except ProviderError as exc:
                error = exc
            finally:
                DURATION.labels(self.name).observe(clock.monotonic() - started)
            ERRORS.labels(self.name, error.status).inc()
            self.failures += 1
            if self.failures >= self.settings.circuit_threshold or error.status in (
                Status.CLOUDFLARE,
                Status.CAPTCHA,
                Status.RATE_LIMIT,
            ):
                self.blocked_until = max(
                    self.blocked_until, clock.monotonic() + self.settings.circuit_cooldown
                )
            # Remove stuck loading state; browser/context remain shared.
            page = self.pages.pop("working", None)
            if page and not page.is_closed():
                await page.close()
            raise error

    async def _catalog(self):
        if (
            self.centers is not None
            and clock.monotonic() - self.centers_fetched < self.settings.catalog_ttl
        ):
            return self.centers
        page = await self._page(ENTRY, refresh=True)
        # Embedded JSON is the source, not rendered slots.
        await page.locator('[x-data^="{items:"]').first.wait_for(state="attached")
        self.centers = parse_centers(await page.content())
        self.centers_fetched = clock.monotonic()
        return self.centers

    async def get_locations(self):
        departments = await self._operation(self._catalog)
        return list(
            {
                d.location_id: Location(d.location_id, d.city, city=d.city) for d in departments
            }.values()
        )

    async def get_departments(self, location):
        departments = await self._operation(self._catalog)
        if location.region_id:
            if any(d.city not in DOCUMENT_REGIONS for d in departments):
                raise ProviderError(
                    Status.PARSING_ERROR, "New Document city needs administrative region mapping"
                )
            departments = [d for d in departments if DOCUMENT_REGIONS[d.city] == location.region_id]
        return [
            d
            for d in departments
            if not location.city or normalize(d.city) == normalize(location.city)
        ]

    async def get_services(self, department):
        async def action():
            page = await self._page(department.booking_url, refresh=True)
            await page.locator("#service").wait_for(state="attached")
            return parse_services(await page.content())

        return await self._operation(action)

    async def _response(self, page, selector, value):
        await page.wait_for_function(
            "document.querySelector('#services')?._x_dataStack?.length > 0"
        )
        form = "days" if selector == "#service" else "times"
        async with page.expect_response(
            lambda r: (
                r.request.method == "POST"
                and r.url == page.url
                and bool(
                    re.search(r'name="form"\s*\r?\n\r?\n' + form + r"\b", r.request.post_data or "")
                )
            ),
            timeout=self.settings.http_timeout * 1000,
        ) as pending:
            # Reset ensures the change event fires on repeated polling of the same service/date.
            if selector == "#service":
                await page.locator(selector).select_option("")
            await page.locator(selector).select_option(value)
        response = await pending.value
        if response.status in (403, 429, 503):
            self._respect_retry_after(response)
            await self._challenge(page)
            raise ProviderError(Status.RATE_LIMIT, f"Browser availability HTTP {response.status}")
        if response.status != 200:
            raise ProviderError(
                Status.PROVIDER_ERROR, f"Browser availability HTTP {response.status}"
            )
        try:
            return await response.json()
        except (ValueError, BrowserError) as exc:
            raise ProviderError(
                Status.PARSING_ERROR, "Provider contract changed: non-JSON response"
            ) from exc

    async def get_available_dates(self, department, service, start, end):
        async def action():
            page = await self._page(department.booking_url)
            parse_services(await page.content())
            days = parse_days(await self._response(page, "#service", service.id))
            return [d for d in days if start <= d <= end]

        return await self._operation(action)

    async def get_available_slots(self, department, service, day):
        async def action():
            page = await self._page(department.booking_url)
            if await page.locator("#service").input_value() != service.id:
                parse_days(await self._response(page, "#service", service.id))
            data = await self._response(page, "#date", day.isoformat())
            return parse_times(data, department, service, day)

        return await self._operation(action)

    async def close(self):
        context, browser, runtime = self.context, self.browser, self.runtime
        self.context = self.browser = self.runtime = None
        self.pages.clear()
        # Closed browser objects can raise during cleanup; still release the driver.
        if context:
            with suppress(BrowserError):
                await context.close()
        if browser:
            with suppress(BrowserError):
                await browser.close()
        if runtime:
            await runtime.stop()

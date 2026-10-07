import json
import logging

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("queue_provider_requests_total", "Provider HTTP attempts", ["provider"])
ERRORS = Counter("queue_provider_errors_total", "Provider errors", ["provider", "status"])
DURATION = Histogram("queue_provider_response_seconds", "Provider latency", ["provider"])
SLOTS = Gauge("queue_available_slots", "Latest available slots", ["provider", "job"])
NOTIFICATIONS = Counter("queue_notifications_total", "Delivered Telegram messages")
SUBSCRIPTIONS = Gauge("queue_active_subscriptions", "Enabled subscriptions")
LAST_SUCCESS = Gauge("queue_last_success_timestamp", "Last successful poll", ["provider"])


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"event": record.getMessage(), "level": record.levelname}
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # HTTP client logs can contain Telegram tokens in URLs.
    for name in ("httpx", "httpcore", "aiogram.event", "aiohttp.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

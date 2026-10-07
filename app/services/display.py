PROVIDER_TITLES = {"dmsu": "ДМСУ", "document": "ДП «Документ»"}
STATUS_TITLES = {
    "NEW": "очікується перша перевірка",
    "AVAILABLE": "є вільні місця",
    "NO_SLOTS": "вільних місць немає",
    "PROVIDER_ERROR": "помилка сервісу",
    "PARSING_ERROR": "формат відповіді сервісу змінився",
    "RATE_LIMIT": "сервіс обмежив запити",
    "CLOUDFLARE": "захист Cloudflare заблокував перевірку",
    "CAPTCHA": "сервіс вимагає CAPTCHA",
    "AUTH_REQUIRED": "сервіс вимагає підтвердження особи",
    "TIMEOUT": "сервіс не відповів вчасно",
    "BROWSER_ERROR": "помилка браузерного доступу",
}


def status_title(status):
    return STATUS_TITLES.get(status, "стан перевірки невідомий")


def readable_errors(errors):
    parts = [e.split(": ", 1) for e in errors]
    return "; ".join(f"{PROVIDER_TITLES.get(p, p)} — {status_title(s)}" for p, s in parts)

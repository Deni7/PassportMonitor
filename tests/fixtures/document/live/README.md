# Публічні відповіді ДП «Документ», 2026-10-06

Отримані `python -m app.check_document` у production контейнері Podman:
звичайний Chromium + Xvfb, без бронювання та введення персональних даних.
Збережені тільки поля доступності; cookies, CSRF, headers та HTML не зберігалися.

| Файл | Центр / послуга |
|---|---|
| `01-days.json` | lviv2.pasport.org.ua / паспорт та ID, порожні dates |
| `02-days.json` | komod.pasport.org.ua / паспорт та ID, порожні dates |
| `03-days.json` | respublika.pasport.org.ua / паспорт та ID, порожні dates |
| `04-days.json` | gotovo.pasport.org.ua / паспорт та ID, порожні dates |
| `05-days.json` | gotovo.pasport.org.ua / водійське посвідчення, три дати |
| `06-timeSlots.json` | та сама послуга, 2026-10-08: шість часових вікон, по 14 місць |

Це реальні captures, не synthetic fixtures. Дані відображають момент перевірки,
а не поточну доступність. Подальше опитування решти Київських центрів зупинилося
на RATE_LIMIT / HTTP 429; помилка не трактувалася як порожній результат.

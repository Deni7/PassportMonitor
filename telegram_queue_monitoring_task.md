# Технічне завдання: Telegram-бот моніторингу електронних черг ДМСУ та ДП «Документ»

# Постановка задачі

Необхідно розробити production-ready Telegram-бота для автоматичного моніторингу доступних місць в електронних чергах:

1. Державної міграційної служби України — ДМСУ.
2. ДП «Документ» / Паспортний сервіс.

Бот повинен працювати автономно в Docker-контейнері та дозволяти користувачу через Telegram обрати:

- тип установи: ДМСУ, ДП «Документ» або обидві одночасно;
- населений пункт / регіон;
- конкретну паспортну послугу;
- за необхідності додаткові параметри послуги;
- період або допустимий діапазон дат.

Після налаштування бот повинен автоматично моніторити **всі доступні підрозділи обраних установ у вибраному населеному пункті/регіоні** та негайно повідомляти користувача про появу доступного місця.

У повідомленні обов'язково мають бути:

- установа;
- назва підрозділу;
- адреса;
- назва послуги;
- дата;
- час, якщо він доступний;
- кількість доступних слотів, якщо ця інформація доступна;
- час виявлення;
- кнопка або URL для переходу безпосередньо до офіційної сторінки реєстрації в електронну чергу.

Бот **не повинен автоматично бронювати місце**, проходити CAPTCHA, BankID, Дію, SMS-підтвердження або інші механізми автентифікації. Його функція — лише моніторинг і максимально швидке сповіщення.

---

# 1. Попереднє дослідження

Перед написанням основного коду необхідно дослідити актуальну реалізацію обох сервісів.

Для кожного з них визначити:

- офіційну URL-адресу електронної черги;
- спосіб отримання списку регіонів/населених пунктів;
- спосіб отримання списку підрозділів;
- спосіб отримання списку послуг;
- спосіб отримання доступних дат;
- спосіб отримання доступних часових слотів;
- які HTTP API/XHR/fetch/WebSocket запити використовує frontend;
- необхідні headers, cookies, CSRF-токени та інші параметри;
- чи можна виконувати запити без браузера;
- чи застосовується Cloudflare, CAPTCHA, JavaScript challenge або інший anti-bot захист;
- чи змінюються endpoint-и залежно від регіону;
- чи існують окремі backend API для різних підрозділів.

Пріоритет методів отримання інформації:

1. Прямий офіційний HTTP API.
2. HTTP-запити до endpoint-ів, які використовує офіційний frontend.
3. Headless Chromium / Playwright лише якщо прямий HTTP-доступ неможливий або нестабільний.

Не використовувати HTML scraping там, де ті самі дані доступні через JSON API.

Результати дослідження оформити окремо в `docs/providers.md`.

---

# 2. Архітектура

Застосунок необхідно побудувати модульно.

Основні компоненти:

```text
Telegram Bot
     │
     ▼
Application Layer
     │
     ├── Subscription Manager
     ├── Monitoring Scheduler
     ├── Notification Manager
     └── State / Deduplication
                │
                ▼
        Provider Interface
        ┌───────┴────────┐
        │                │
   DMSUProvider    DocumentProvider
        │                │
        ▼                ▼
     ДМСУ API       ДП Документ API
```

Provider-и не повинні містити Telegram-логіки.

Визначити єдиний інтерфейс провайдера, наприклад:

```python
class QueueProvider:
    async def get_locations(...)
    async def get_departments(...)
    async def get_services(...)
    async def get_available_dates(...)
    async def get_available_slots(...)
    async def get_booking_url(...)
```

Обидві системи повинні повертати дані у внутрішній нормалізованій моделі.

Наприклад:

```text
Provider
Region
City
Department
Service
AvailableDate
AvailableSlot
```

При зміні API одного провайдера не повинна ламатися логіка Telegram-бота або іншого провайдера.

---

# 3. Рекомендований стек

Бажаний стек:

- Python 3.12+;
- `aiogram 3.x` для Telegram;
- `asyncio`;
- `httpx` або `aiohttp`;
- Playwright як optional fallback;
- PostgreSQL для постійного стану;
- Redis — опціонально для locks/cache;
- SQLAlchemy 2.x;
- Alembic;
- Pydantic Settings;
- structured logging;
- Docker;
- Docker Compose.

Допускається обґрунтовано запропонувати інший стек, але архітектура повинна залишатися асинхронною та придатною для одночасного моніторингу великої кількості підрозділів.

---

# 4. Telegram UX

При `/start` показувати головне меню.

Приклад сценарію:

```text
/start

Що моніторимо?

[ ДМСУ ]
[ ДП «Документ» ]
[ ДМСУ + ДП «Документ» ]
```

Далі:

```text
Оберіть область / місто
```

Після цього:

```text
Оберіть послугу
```

Наприклад:

```text
Закордонний паспорт:
○ оформлення
○ обмін
○ термінове оформлення
```

Не hardcode-ити перелік послуг, якщо його можна отримати від провайдера.

Якщо назви аналогічних послуг у ДМСУ та ДП «Документ» відрізняються, необхідно створити mapping між:

```text
CanonicalService
        │
        ├── DMSU service ID
        └── Document service ID
```

Користувач повинен обирати логічну послугу один раз.

---

# 5. Моніторинг усіх підрозділів

Критична вимога:

Якщо користувач вибрав, наприклад, Львів, бот повинен перевіряти **не один підрозділ**, а всі підрозділи ДМСУ та/або ДП «Документ», які:

- розташовані у вибраному місті або заданій зоні;
- надають обрану послугу;
- підтримують електронний запис.

При появі слота в будь-якому з них користувач отримує повідомлення.

Приклад:

```text
🟢 З'явилося вільне місце

Установа: ДМСУ
Послуга: Закордонний паспорт
Підрозділ: Галицький відділ
Адреса: ...
Дата: 09.10.2026
Час: 14:20

Виявлено: 20:14:37

[ Записатися ]
```

---

# 6. Частота моніторингу

Частота перевірки має бути конфігурованою окремо для кожного provider-а.

Не створювати агресивне опитування.

Базове значення:

```text
normal interval:
30–90 секунд
```

Додати random jitter, наприклад:

```text
±10–20 %
```

щоб усі запити не виконувалися синхронно.

Для ДМСУ передбачити optional режим підвищеної частоти перевірок навколо часу появи нового дня в електронній черзі.

Наприклад:

```text
23:55–00:10 → fast interval
решта часу → normal interval
```

Але фактичну поведінку необхідно винести в конфігурацію.

---

# 7. Оптимізація кількості запитів

Не виконувати один і той самий HTTP-запит окремо для кожного Telegram-користувача.

Приклад:

```text
20 користувачів
       │
       │ всі моніторять Львів / Закордонний паспорт
       ▼
одне polling-завдання
       │
       ▼
результат
       │
       ├── user1
       ├── user2
       ├── user3
       └── ...
```

Необхідно групувати subscriptions за:

```text
provider
location
service
department/filter
```

та кешувати результат одного polling cycle.

---

# 8. Deduplication

Не надсилати користувачу одне й те саме повідомлення кожні 30 секунд.

Для кожного доступного слота сформувати stable fingerprint, наприклад:

```text
provider
department_id
service_id
date
time
```

Зберігати історію вже повідомлених слотів.

Повторно повідомляти тільки якщо:

- слот зник;
- після певного часу з'явився знову;

або якщо змінився його стан.

Реалізувати state machine приблизно:

```text
NEW
AVAILABLE
NOTIFIED
DISAPPEARED
AVAILABLE_AGAIN
```

---

# 9. Поведінка при появі кількох слотів

Якщо одночасно доступно багато слотів, не надсилати десятки Telegram-повідомлень.

Групувати результат.

Наприклад:

```text
🟢 ДП «Документ» — Львів

Підрозділ: Паспортний сервіс №...
Доступні місця:

07.10
 • 14:30
 • 15:10
 • 16:20

08.10
 • 09:40
 • 10:20

[ Перейти до запису ]
```

---

# 10. Booking URL

Для кожного повідомлення сформувати максимально корисне посилання.

Порядок пріоритетів:

1. deep link прямо на підрозділ/послугу;
2. URL із query/path параметрами;
3. сторінка конкретного підрозділу;
4. загальна сторінка електронної черги.

Не вигадувати URL.

Посилання повинно бути перевірене та вести лише на офіційний домен відповідного сервісу.

---

# 11. База даних

Мінімальні таблиці:

```text
users
subscriptions
providers
locations
departments
services
provider_services
poll_jobs
slots
notifications
provider_health
```

`subscriptions` повинна містити як мінімум:

```text
id
telegram_user_id
provider_mode
location_id
canonical_service_id
date_from
date_to
enabled
created_at
```

---

# 12. Керування підписками

Telegram-бот повинен дозволяти:

```text
➕ Додати моніторинг
📋 Мої моніторинги
⏸ Призупинити
▶️ Відновити
🗑 Видалити
🔄 Перевірити зараз
```

Команда `Перевірити зараз` не повинна обходити rate-limit або створювати окремий flood запитів до provider-а.

Якщо свіжі дані є в cache — використати їх.

---

# 13. Health monitoring

Для кожного provider-а відстежувати:

```text
last_success
last_error
response_time
consecutive_failures
last_available_slots
```

Якщо API провайдера змінився або повертає неочікувану структуру, застосунок не повинен мовчки вважати, що:

```text
0 slots
```

Необхідно розрізняти:

```text
NO_SLOTS
PROVIDER_ERROR
PARSING_ERROR
RATE_LIMIT
CLOUDFLARE
TIMEOUT
```

---

# 14. Адміністративні сповіщення

Передбачити `ADMIN_TELEGRAM_IDS`.

Адміністратор повинен отримувати повідомлення, якщо:

- API одного з provider-ів не працює;
- змінився JSON schema;
- більше N перевірок поспіль завершились помилкою;
- виник Cloudflare challenge;
- відбулася помилка Playwright;
- Telegram API недоступний.

Не надсилати адміну повідомлення при кожній одиничній timeout-помилці.

Використати threshold/cooldown.

---

# 15. Browser fallback

Якщо один із сайтів неможливо стабільно перевіряти HTTP-запитами, реалізувати Playwright provider backend.

У Docker використовувати офіційно підтримуваний Chromium.

Не запускати новий browser process для кожної перевірки.

Схема:

```text
Browser
 └── Context
      ├── Page DMSU
      └── Page Document
```

або окремі context-и, якщо це необхідно через cookies/session.

Не використовувати browser automation для обходу CAPTCHA.

Якщо з'явилася CAPTCHA або challenge, provider повинен повернути спеціальний статус.

---

# 16. Rate limiting

Обов'язково реалізувати:

- global rate limit;
- per-provider rate limit;
- exponential backoff;
- random jitter;
- retry policy;
- timeout;
- circuit breaker.

При HTTP:

```text
429
403
503
```

не продовжувати агресивні запити.

---

# 17. Безпека

Не зберігати:

- BankID credentials;
- Дія credentials;
- SMS-коди;
- паспортні дані;
- банківські дані.

Telegram token, DB password та інші secrets передавати лише через environment variables / Docker secrets.

Не логувати cookies, authorization headers або персональні дані.

---

# 18. Docker

Проєкт повинен запускатися:

```bash
docker compose up -d
```

Необхідні сервіси:

```yaml
services:
  bot:
  postgres:
```

Якщо потрібен Redis:

```yaml
  redis:
```

Якщо Playwright потребує окремого worker-а, допустимо:

```yaml
  browser-worker:
```

Контейнери повинні мати:

- healthcheck;
- restart policy;
- persistent volume для PostgreSQL;
- timezone `Europe/Kyiv`.

---

# 19. Configuration

Через `.env`:

```env
TELEGRAM_BOT_TOKEN=
ADMIN_TELEGRAM_IDS=

DATABASE_URL=

DMSU_ENABLED=true
DOCUMENT_ENABLED=true

DMSU_POLL_INTERVAL=
DOCUMENT_POLL_INTERVAL=

HTTP_TIMEOUT=
MAX_RETRIES=
BACKOFF_FACTOR=

LOG_LEVEL=INFO
TZ=Europe/Kyiv
```

Створити `.env.example`.

Secrets у git не додавати.

---

# 20. Логування

Structured logs.

Наприклад:

```json
{
  "event": "provider_poll",
  "provider": "dmsu",
  "department": "123",
  "service": "passport",
  "duration_ms": 532,
  "status": "success",
  "slots": 3
}
```

Не перетворювати normal `NO_SLOTS` на ERROR.

---

# 21. Метрики

Підготувати `/metrics` endpoint у форматі Prometheus.

Мінімальні metrics:

```text
queue_provider_requests_total
queue_provider_errors_total
queue_provider_response_seconds
queue_available_slots
queue_notifications_total
queue_active_subscriptions
queue_last_success_timestamp
```

---

# 22. Тести

Потрібні unit-тести для:

- provider parsing;
- service mapping;
- slot fingerprint;
- deduplication;
- subscription matching;
- notification grouping;
- retry/backoff.

Окремо зберегти anonymized fixtures реальних відповідей API:

```text
tests/fixtures/dmsu/
tests/fixtures/document/
```

Тести не повинні залежати від доступності production-сайтів.

---

# 23. Integration tests

Створити optional integration tests:

```bash
pytest -m integration
```

які перевіряють read-only доступ до реальних provider-ів.

Вони не повинні виконуватись автоматично у звичайному unit test pipeline.

---

# 24. Provider contract tests

Критично важливо створити перевірку фактичної схеми відповіді provider-а.

Наприклад:

```text
expected:
departments: list
service_id: str/int
date: ISO date
slots: list
```

Якщо production API змінив формат, тест/health check повинен явно повідомити:

```text
Provider contract changed
```

замість того щоб трактувати це як відсутність слотів.

---

# 25. Graceful restart

Після:

```text
docker restart
```

не втрачати:

- користувачів;
- subscriptions;
- state доступних слотів;
- інформацію про вже надіслані notification.

Не надсилати повторно всі старі slots після кожного restart.

---

# 26. Time zones

Усі timestamps усередині системи зберігати в UTC.

Користувачу показувати:

```text
Europe/Kyiv
```

Дати/час слотів трактувати відповідно до timezone установи, якщо provider не передає timezone явно.

---

# 27. Структура репозиторію

Бажана структура:

```text
app/
├── bot/
│   ├── handlers/
│   ├── keyboards/
│   └── middlewares/
│
├── providers/
│   ├── base.py
│   ├── dmsu/
│   └── document/
│
├── monitoring/
│   ├── scheduler.py
│   ├── polling.py
│   ├── dedup.py
│   └── notifier.py
│
├── models/
├── repositories/
├── services/
├── config/
└── main.py

alembic/
tests/
docs/

Dockerfile
docker-compose.yml
.env.example
pyproject.toml
README.md
```

---

# 28. README

README має містити:

1. Опис архітектури.
2. Як створити Telegram bot через BotFather.
3. Налаштування `.env`.
4. Docker deployment.
5. Міграції БД.
6. Debugging provider-ів.
7. Як додати новий provider.
8. Як оновити service mapping.
9. Як перевірити health.
10. Типові проблеми з Cloudflare/rate limiting.

---

# 29. Критична вимога до реалізації

Не починати написання provider-а, виходячи з припущень щодо API.

Спочатку:

```text
DISCOVER
    ↓
DOCUMENT
    ↓
CAPTURE REAL RESPONSES
    ↓
IMPLEMENT
    ↓
TEST
```

Усі endpoint-и, payload-и та response schema повинні базуватися на фактичній поточній реалізації офіційних сайтів.

Не вигадувати endpoint-и.

Якщо отримати певні дані без browser automation неможливо — явно це зафіксувати і реалізувати browser fallback.

---

# 30. Обмеження

Заборонено:

- автоматично бронювати талони;
- обходити CAPTCHA;
- обходити Cloudflare anti-bot;
- маскувати бот під тисячі клієнтів;
- використовувати proxy rotation для обходу rate-limit;
- автоматично проходити SMS verification;
- виконувати BankID/Дія authentication;
- збирати або зберігати паспортні дані користувача.

Система повинна поводитися як read-only availability monitor.

---

# 31. Очікуваний результат

Після запуску:

```bash
docker compose up -d
```

користувач повинен мати можливість через Telegram:

```text
Додати моніторинг

→ ДМСУ + ДП «Документ»
→ Львів
→ Оформлення закордонного паспорта
→ будь-який підрозділ
```

Після цього система сама визначає всі відповідні підрозділи обох provider-ів і періодично перевіряє їх.

При появі місця користувач одразу отримує:

```text
🟢 Є вільне місце

🏢 ДП «Документ»
📍 Львів — Паспортний сервіс ...
📌 Адреса: ...

📄 Закордонний паспорт
📅 08.10.2026
🕑 14:40

Виявлено: 20:15:31

[ Записатися ]
```

Якщо через кілька секунд з'явиться місце в іншому відділенні — повідомити також про нього.

---

# 32. Порядок виконання задачі

Роботу виконувати поетапно.

## Phase 1 — Discovery

Дослідити ДМСУ та ДП «Документ».

Надати:

```text
docs/providers.md
```

з описом реальних endpoint-ів, flow, cookies/tokens, response schemas та anti-bot механізмів.

## Phase 2 — Architecture

Створити:

- data models;
- Provider interface;
- DB schema;
- migration;
- Telegram skeleton.

## Phase 3 — DMSU Provider

Реалізувати та протестувати ДМСУ.

## Phase 4 — Document Provider

Реалізувати та протестувати ДП «Документ».

## Phase 5 — Monitoring Engine

Реалізувати:

- polling;
- scheduler;
- caching;
- deduplication;
- subscription fan-out;
- notification grouping.

## Phase 6 — Telegram UX

Реалізувати повний flow створення та керування subscriptions.

## Phase 7 — Containerization

Створити:

- Dockerfile;
- docker-compose.yml;
- healthchecks;
- persistent DB.

## Phase 8 — Tests

Unit + provider contract + optional integration tests.

## Phase 9 — Documentation

README та документація provider-ів.

---

# 33. Правила роботи LLM над кодом

Не генерувати весь проєкт одним великим кроком.

На кожному етапі:

1. дослідити проблему;
2. показати знайдені факти;
3. визначити архітектурне рішення;
4. реалізувати код;
5. запустити tests/linter;
6. виправити помилки;
7. тільки після цього переходити до наступного етапу.

Не залишати:

```text
TODO
mock
placeholder
fake endpoint
example response
```

у production implementation.

Якщо зовнішній API неможливо перевірити — чітко вказати, яка частина залишається неперевіреною.

---

# Definition of Done

Проєкт вважається завершеним, якщо:

- бот запускається через Docker Compose;
- PostgreSQL зберігає subscriptions і monitoring state;
- ДМСУ реально перевіряється;
- ДП «Документ» реально перевіряється;
- користувач може вибрати одну логічну послугу;
- система перевіряє всі відповідні підрозділи міста;
- одна підписка може одночасно охоплювати ДМСУ + ДП «Документ»;
- знайдені slots коректно нормалізуються;
- дублікати notifications не надсилаються;
- є робоче офіційне booking URL;
- restart контейнера не спричиняє повторного spam;
- provider failure не інтерпретується як «місць немає»;
- реалізовані rate-limit/backoff;
- є healthcheck;
- є Prometheus metrics;
- є unit tests;
- є provider contract tests;
- документація відповідає фактичній реалізації.

---

## Додаткова архітектурна вимога

Бот не повинен бути прив'язаний саме до паспортів. `CanonicalService` + `Provider` мають дозволяти надалі додавати інші послуги без переписування monitoring engine.

Для першої реалізації рекомендований стек: **Python + aiogram + httpx + PostgreSQL**, а Playwright використовувати лише як fallback.

# Монітор електронних черг

Асинхронний Telegram-бот для read-only моніторингу ДМСУ та ДП «Документ».
Користувач обирає установи, область/місто, логічну послугу й діапазон дат.
Бот перевіряє всі відповідні підрозділи, групує знайдені місця й дає офіційне
посилання для самостійного запису. Бронювання та автентифікація не виконуються.

**Статус перевірки:** HTTP інтеграційний тест ДМСУ пройдено; збережено каталог
365 підрозділів із 22 регіонів. Позитивні JSON зі слотами ДМСУ ще не отримано.
ДП «Документ» перевірено в звичайному Chromium із Xvfb: отримано реальні
JSON дат та часу, включно з доступними місцями для водійських посвідчень
у Києві. Headless Chromium отримував CLOUDFLARE, тому типовий режим —
`DOCUMENT_BROWSER_HEADLESS=false`. Окремі центри можуть повертати 429;
Retry-After та circuit breaker зупиняють запити. Деталі та фактичні
endpoint-и: [docs/providers.md](docs/providers.md). Результати перевірок:
[docs/verification.md](docs/verification.md).

## Архітектура

`app/bot.py` — aiogram FSM, український UX, пагінація, створення й керування
підписками. `services/catalog.py` — TTL-кеш каталогів, усі підрозділи міста або
області. `services/mapping.py` — логічні послуги та відповідність реальним назвам.
Інтерфейс `providers/base.py` не залежить від Telegram.

Об'єднана послуга ДМСУ «Паспорт громадянина України для виїзду за кордон,
або у формі картки (ID)» входить у два логічні вибори: закордонний паспорт та
ID-картка. Окрема третя кнопка не створюється; об'єднаний підрозділ опитується
для будь-якого з цих виборів.

ДМСУ використовує httpx; ДП «Документ» — один Playwright Chromium, context і
обмежений кеш із двох pages. У контейнері Chromium працює зі звичайним
графічним режимом на приватному Xvfb display; `xauth` та init включені.
HTTP отримував 403; CAPTCHA/challenge зупиняє роботу. JSON availability отримується
через responses офіційного frontend. Дати/час не scrape-яться з HTML.

Scheduler формує одне завдання на `provider/department/service`, об'єднує
діапазони дат усіх підписників, зберігає deadline й результат у PostgreSQL.
ДМСУ має обмежену concurrency, ДП «Документ» опитується послідовно.
HTTP-запити проходять спільний та окремий для провайдера rate limit.
Jitter, timeout, retries, circuit breaker і backoff не залежать від кількості
користувачів. Кнопка «Перевірити зараз» читає кеш і не змінює deadline.

Постійна state machine слотів: `NEW → AVAILABLE → NOTIFIED → DISAPPEARED →
AVAILABLE_AGAIN`. Повторна поява після cooldown або зміна кількості місць
створює нове покоління; unique constraint захищає від повтору для користувача,
навіть коли його підписки перекриваються. Помилки/часткові результати не
позначають відомі слоти як зниклі.

## Запуск

Потрібні Docker Engine + Compose v2 або Podman + podman-compose.
Перевірено Podman 5.8.2 / podman-compose 1.6.0 у Linux WSL ВМ.
Створіть бота через офіційного
[@BotFather](https://t.me/BotFather), командою `/newbot`, і скопіюйте token.

```bash
cp .env.example .env
# Заповніть TELEGRAM_BOT_TOKEN і POSTGRES_PASSWORD.
# Задайте NPM_NETWORK — назву наявної мережі Nginx Proxy Manager.
# Якщо мережі ще немає: docker network create npm
docker compose up -d --build
docker compose logs -f bot
```

Для Podman у PowerShell:

```powershell
Copy-Item .env.example .env # Лише для нового deployment; не перезаписуйте готовий .env.
# Заповніть секрети. Якщо 8080 зайнятий, задайте METRICS_PORT=18080.
podman compose -p passport-monitor -f docker-compose.yml up -d --build
podman compose -p passport-monitor -f docker-compose.yml logs -f bot
Invoke-RestMethod http://127.0.0.1:18080/health # Для METRICS_PORT=18080.
```

Поточний deployment: [@uapass_bot](https://t.me/uapass_bot), health/metrics на
`127.0.0.1:18080`. Відкрийте чат і надішліть `/start`, щоб створити підписку.
Обидві установи ввімкнені. ДП «Документ» використовує перевірений режим Xvfb;
provider error показується окремо від відсутності місць.
Бот і БД мають `restart: unless-stopped`; сама Podman ВМ також має бути запущена.

Для стандартного Compose deployment задайте пароль лише в `POSTGRES_PASSWORD`:
бот і Alembic автоматично сформують URL, включно з кодуванням спецсимволів.
Значення з `$` у `.env` беріть в одинарні лапки, щоб Compose не підставляв змінні.
`DATABASE_URL` — необов'язкове перевизначення для іншої БД; якщо воно задане,
використовується саме цей URL. Після оновлення видаліть старий `DATABASE_URL`
із `.env`, щоб перейти на спільний пароль. У явному URL пароль потрібно
percent-encode. Зміна `POSTGRES_PASSWORD` не змінює пароль у вже створеній БД:
для цього використайте `\password monitor` у psql, не видаляючи volume.
`ADMIN_TELEGRAM_IDS` — числові ID через
кому. Кожен адміністратор має спочатку відкрити бота й надіслати `/start`, щоб
Telegram дозволив надсилати йому повідомлення.

Secret token, cookie, headers та персональні поля не логуються. `.env` виключено
з git і Docker build context. API клієнтів не виконує customer/book, sms, orders,
BankID/Дія. Browser request guard дозволяє POST тільки `form=days|times` і
блокує зовнішню аналітику/збір fingerprint.

PostgreSQL не публікується на host; persistent volume `postgres-data` зберігає
стан після restart. `docker compose down` зберігає volume; `down -v` видаляє БД.
Health/metrics публікуються тільки на `127.0.0.1:8080`. Часова зона установ —
`Europe/Kyiv`, усі внутрішні timestamps — UTC.

Compose явно підключає бот і PostgreSQL до внутрішньої мережі `backend`.
Бот також підключений до зовнішньої мережі `proxy`, фактична назва якої
задається через `NPM_NETWORK` у `.env` (типово `npm`). Вкажіть назву наявної
мережі Nginx Proxy Manager; її можна переглянути в Portainer або через
`docker network ls`. Ця мережа має існувати перед запуском Compose.
Для локального запуску без NPM її можна створити командою
`docker network create npm` (для Podman — `podman network create npm`).

У NPM задайте Scheme `http`, Forward Hostname `passport-monitor` та
Forward Port зі значення `METRICS_PORT` (наприклад, `18080`). Відкривайте
`/dashboard`. Alias `passport-monitor` залишається стабільним після
пересоздання контейнера; PostgreSQL підключений лише до `backend`.
Після зміни мереж застосуйте `docker compose up -d`; Compose пересоздасть
контейнери з потрібними підключеннями, зберігаючи volume БД.

## Налаштування

| Змінна | Типове значення / призначення |
|---|---|
| `DMSU_ENABLED`, `DOCUMENT_ENABLED` | ввімкнення провайдерів |
| `DOCUMENT_BROWSER_HEADLESS` | false; звичайний Chromium на Xvfb, true — headless |
| `DMSU_POLL_INTERVAL`, `DOCUMENT_POLL_INTERVAL` | 60 / 90 секунд, мінімум 30 |
| `JITTER` | 0.15, максимум 0.2 |
| `DMSU_FAST_ENABLED` | false; optional локальне вікно 23:55–00:10 |
| `DMSU_FAST_INTERVAL` | 30 секунд; вікно конфігурується START/END |
| `GLOBAL_REQUEST_INTERVAL`, `PROVIDER_REQUEST_INTERVAL` | 1 / 2 секунди між запитами |
| `MAX_CONCURRENT_JOBS` | 4 для HTTP, browser завжди 1 |
| `HTTP_TIMEOUT`, `MAX_RETRIES`, `BACKOFF_FACTOR` | 20 секунд / 2 / 2 |
| `CIRCUIT_THRESHOLD`, `CIRCUIT_COOLDOWN` | 5 помилок / 300 секунд |
| `ADMIN_FAILURE_THRESHOLD`, `ADMIN_COOLDOWN` | 3 / 1800 секунд |
| `REAPPEARANCE_COOLDOWN` | 600 секунд від моменту зникнення |
| `CATALOG_TTL` | 21600 секунд; помилки каталогу кешуються 60 секунд |

Poll interval — бажаний мінімальний інтервал завдання. Великий регіон або багато
доступних дат можуть збільшити фактичний час циклу через rate limit. Підвищення
concurrency не обходить обмеження частоти запитів.

Діапазон підписки: до 90 днів, не раніше поточного дня, не пізніше року наперед.
Додаткові паспортні параметри не пропонуються: перевірені публічні каталоги
availability не розрізняють терміновість/обмін. ДП «Документ» має спільну чергу
закордонного паспорта та ID; UI дозволяє один логічний вибір із цією відповідністю.

## Міграції та локальна розробка

Міграція `0001_initial.py` фіксує основні таблиці; `0002_dashboard_analytics.py`
додає профілі користувачів, журнал подій, історію перевірок і запитів.
Startup виконує `alembic upgrade head`.
DDL для PostgreSQL збережено в `docs/schema.sql`.

```bash
python -m venv .venv
# Активуйте .venv відповідно до вашої ОС.
pip install -e '.[dev]'
python -m playwright install chromium
export DATABASE_URL='postgresql+asyncpg://monitor:password@localhost:5432/monitor'
python -m alembic upgrade head
python -m app.main
```

Для PowerShell: `$env:DATABASE_URL = '...'`. Бот і Alembic читають налаштування
з оточення та `.env`; оточення має пріоритет. Для локальної БД задайте
`DATABASE_URL`, оскільки автоматичний URL використовує Compose-host `postgres`.
Docker встановлює точні версії з `requirements.lock` та Chromium відповідної
версії офіційного Playwright. Контейнер працює як непривілейований користувач.

Один процес володіє PostgreSQL advisory lock; друга репліка завершується до
Telegram polling. Втрата lock connection завершує процес. Docker restart policy
відновлює його. Для горизонтального масштабування слід відокремити workers і
додати leases/outbox claims; поточний deployment — один bot process.

## Тести

```bash
pytest
ruff check app tests alembic scripts
ruff format --check app tests alembic scripts
# Linux: потрібен графічний display або Xvfb.
xvfb-run -a pytest -m integration
```

Acceptance із PostgreSQL в окремому контейнері, без production `.env` і Telegram:

```powershell
podman compose -p passport-monitor-acceptance -f compose.verification.yml build
podman compose -p passport-monitor-acceptance -f compose.verification.yml up -d postgres
podman compose -p passport-monitor-acceptance -f compose.verification.yml run --rm -T verification
# Read-only перевірка реальних провайдерів:
podman compose -p passport-monitor-acceptance -f compose.verification.yml run --rm -T verification xvfb-run -a python -m pytest -q -p no:cacheprovider -m integration tests/test_integration.py
podman compose -p passport-monitor-acceptance -f compose.verification.yml down
```

`TEST_DATABASE_URL` перемикає repository-тести зі SQLite на PostgreSQL:
кожен тест створює окрему schema та видаляє її після завершення. Використовуйте
тільки окрему тестову БД із застосованою міграцією. Production image не містить
pytest або тестів; target `verification` додає їх тільки для acceptance.

Звичайні тести offline; fixtures ДМСУ — реальні public JSON, Document fixtures
містять public DOM та реальні positive JSON у `tests/fixtures/document/live/`.
Окремі синтетичні тести перевіряють error branches.
Integration tests не бронюють і не заповнюють персональних полів. Challenge/
timeout/schema change призводить до явного падіння тесту, а не до skip/«0 місць».
Для browser integration потрібен установлений Chromium.

Перевірка конкретних міст із production image, без Telegram і бронювання:

```powershell
podman compose -p passport-monitor -f docker-compose.yml exec -T bot xvfb-run -a python -m app.check_document --city Львів
```

Без `--city` probe перевіряє Львів і Київ; `--city` можна повторити. Не запускайте
probe паралельно зі scheduler для тих самих центрів. Не робіть негайних повторів
після 429: зачекайте щонайменше `CIRCUIT_COOLDOWN` і час із Retry-After.

## Вебдешборд та аналітика поведінки

Дешборд доступний на тому самому порту, що й health/metrics:
`http://127.0.0.1:8080/dashboard` (для поточного налаштування порту 18080 —
`http://127.0.0.1:18080/dashboard`). Встановіть у `.env`:

```dotenv
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your-long-random-password
```

Після перебудови контейнера (`docker compose up -d --build` або відповідна
команда Podman вище) браузер запитає логін і пароль. Без пароля всі сторінки,
assets та API дешборда повертають 503; із неправильними credentials — 401.
Compose залишає порт доступним тільки на localhost. Для віддаленого доступу
використовуйте SSH tunnel або HTTPS reverse proxy: HTTP Basic credentials
потребують захищеного транспортного каналу.

Доступні розділи:

- **Огляд:** активні користувачі, поточні моніторинги, запуски, помилки,
  знайдені слоти, середня тривалість, активність за днями, стан установ,
  популярні команди, географія, послуги, активність за годинами, етапи
  діалогу та поведінкові сигнали (нові користувачі, призупинення, видалення,
  блокування та взаємодії без активного моніторингу).
- **Користувачі:** Telegram ID, ім’я, username, мова, блокування,
  дата реєстрації, остання активність, кількість дій і активних підписок.
  Натискання користувача застосовує його ID до всіх розділів.
- **Моніторинги:** послуга, географія, установи, дати та стан; зберігається
  історія створення, призупинення, відновлення та видалення.
- **Перевірки та запити:** кожен запуск, прив’язка до підписок і користувачів,
  результати слотів, параметри викликів дат/слотів, тривалість і помилки.
  RUNNING видно під час виконання. Один запуск може обслуговувати багато
  підписок. Запит тут — логічний виклик провайдера; HTTP retries і browser
  navigation входять у його тривалість, але не є окремими записами. Каталоги
  не належать до цього журналу; їх збої відображаються у стані установ.
- **Планувальник:** поточні durable jobs, deadline, останній успіх та збої.
  Завдання можуть залишатися після завершення відповідних підписок.
- **Команди й кнопки:** вхідні команди, текст, callback, обрана опція,
  FSM до/після обробки, результат та тривалість. Невідомі дії — UNHANDLED.
- **Повідомлення:** точний текст, кнопки, спроби відправки, Telegram message ID,
  помилки, прив’язка до вхідної події та позицій outbox. SENT означає
  підтвердження Telegram API, а не прочитання користувачем. UNCERTAIN —
  відсутність однозначного підтвердження; автоматичного повтору немає.
- **Черга сповіщень:** кожен слот, його покоління, стан доставки, повтори,
  час наступної спроби та дані пов’язаних підписок.
- **Хронологія:** дії, зміни підписок, перевірки й повідомлення у часовому
  порядку з доступом до повних деталей.

Є фільтри дат, установи, Telegram ID, ID моніторингу, статусу, типу події,
пошук, пагінація, автооновлення кожні 15 секунд і CSV поточної сторінки.
Усі дати фільтра трактуються за Europe/Kyiv; API віддає ISO timestamps з offset.
Дати обмежують історичні події, перевірки й outbox; користувачі, моніторинги
та планувальник показують поточний збережений стан незалежно від періоду.
Для команд/повідомлень фільтри установи й підписки вибирають користувачів,
що мають відповідні підписки; їх загальні команди можуть стосуватися інших
моніторингів. Для перевірок/запитів і outbox прив’язка точна.

Показники етапів — незалежні кількості унікальних користувачів за період,
а не атрибуційна воронка. «Отримали сповіщення» враховує лише доставлені
повідомлення про слоти. Кількість знайдених слотів — сума по запусках, тому
той самий слот може рахуватися повторно. Збережені підписки враховуються
в географії та послугах, включно з видаленими.

Детальна історія накопичується з моменту оновлення; попередні події
не відновлюються. Імена та username оновлюються при наступній взаємодії.
При рестарті незавершені запуски/запити стають INTERRUPTED, а повідомлення
SENDING — UNCERTAIN. Аудит Telegram не змінює правила повторів доставки.
Дані містять персональні профілі та тексти приватних чатів: доступ призначений
для адміністратора. Зберігання наразі без автоматичного видалення; обсяг БД
зростає з кількістю перевірок і результатів.

## Health, метрики та діагностика

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics
docker compose logs --tail=100 bot
docker compose exec bot python -m alembic current
```

`/health` перевіряє БД і progress workers. Provider degradation показується
окремо: last_success, last_error, consecutive_failures, latency та count. HTTP
200 означає, що процес/БД працюють, а не що зовнішні provider-и доступні.
Prometheus: `queue_provider_requests_total`, `queue_provider_errors_total`,
`queue_provider_response_seconds`, `queue_available_slots` (за job),
`queue_notifications_total`, `queue_active_subscriptions`,
`queue_last_success_timestamp`.

Статуси: NO_SLOTS, PROVIDER_ERROR, PARSING_ERROR, RATE_LIMIT, CLOUDFLARE,
CAPTCHA, AUTH_REQUIRED, TIMEOUT, BROWSER_ERROR. На 403/429/503 circuit відкривається
без негайних повторів. При новій schema оновіть парсер і fixtures, виконайте
offline та live contract tests. Не вимикайте challenge і не додавайте stealth,
proxy rotation або автоматичну авторизацію.

## Доставка повідомлень

Слоти одного підрозділу/послуги групуються за датами, максимум 40 на повідомлення
та до ліміту Telegram. Повідомлення містить установу, підрозділ, адресу, послугу,
дати/час, кількість (коли відома), час виявлення й офіційний booking URL.
Підписку можна призупинити/відновити/видалити; pending delivery перевіряє, чи
є хоча б одна активна відповідна підписка. Завершені підписки не опитуються.

Outbox має `PENDING → SENDING → SENT`; 429 і явні API errors повторюються з
backoff. Telegram не підтримує idempotency key для sendMessage: network failure
або аварія між відправкою й commit має невизначений результат. Такий запис стає
`UNCERTAIN` і **не надсилається повторно автоматично**, щоб уникнути restart spam;
адміністратор отримує alert. Перевірте доставку вручну перед поверненням запису
в PENDING. Звичайний restart не повторює SENT. Заблокований користувач має
BLOCKED; `/start` відновлює доступ для наступних повідомлень.

## Розширення

Новий provider реалізує `QueueProvider`, використовує нормалізовані моделі,
спільний Gate, повертає `ProviderError` для збоїв/зміни schema. Додайте його в
main, конфігурацію інтервалу, official_url allowlist, UI modes та integration test.
Monitoring engine не прив'язаний до паспортів.

Для service mapping додайте **точні підтверджені назви** до `ALIASES` у
`app/services/mapping.py` і логічний title до `TITLES`. Не визначайте послугу за
неперевіреним ID: IDs зіставляються з актуальними назвами всередині кожного центру.
Невідомі послуги доступні як provider-specific логічні послуги. Адміністративна
прив'язка міст ДП «Документ» до областей — `app/services/geography.py` (`DOCUMENT_REGIONS`); нове місто
потребує явного mapping для режиму обох установ. При недоступності одного provider-а комбінована підписка зберігає обидві
установи: доступна продовжує працювати, недоступна показує явний status.
Географічні фільтри не є backend IDs. Однойменні міста різних областей
не об'єднуються. Закордонні центри поки виключено (потребують окремих timezone).

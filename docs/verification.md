# Перевірка реалізації — 2026-10-06

| Перевірка | Результат |
|---|---|
| Offline unit/contract/SQLite tests (`pytest -q`) | 43 passed, 1 PostgreSQL test skipped, 2 integration tests deselected |
| Linux container / PostgreSQL acceptance | 44 passed, 2 integration tests deselected |
| Фінальний live acceptance обох провайдерів після cooldown | 2 passed, Chromium + Xvfb, 13.57 секунд |
| `ruff check app tests alembic scripts` | passed |
| `ruff format --check app tests alembic scripts` | passed |
| Alembic upgrade, schema comparison, downgrade | passed на тимчасовій SQLite БД |
| PostgreSQL міграція / schema comparison | passed на PostgreSQL 16, revision `0001`, metadata відповідає БД |
| Live HTTP contract ДМСУ | passed на Windows та у Linux Podman, read-only |
| Каталоги ДМСУ | 22 регіони, 365 підрозділів, реальні JSON fixtures |
| Позитивні відповіді ДМСУ зі слотами | не отримано; поля підтверджені frontend |
| Live Playwright contract ДП «Документ» | звичайний Chromium + Xvfb: passed; headless: CLOUDFLARE |
| Дати/час ДП «Документ» у production | реальні JSON отримано: Львів та 3 центри Києва; позитивні dates/times у центрі «Готово» |
| Подальші запити ДП «Документ» | HTTP 429 у центрі «Україна»; circuit і Retry-After зупиняють запити |
| Compose startup/build | passed: Podman 5.8.2, podman-compose 1.6.0, Linux WSL ВМ |
| PostgreSQL server / advisory lock у runtime | passed: другий процес завершується до Telegram polling; після закриття власника lock знову доступний |
| Рестарт bot / PostgreSQL | passed: БД зберігає revision і provider records; bot автоматично відновив polling після рестарту БД |
| Healthcheck / metrics | passed; process health, stalled worker 503, Prometheus endpoint; deployment `127.0.0.1:18080` |
| Telegram підключення / polling | passed: `@uapass_bot`, реальний token у локальному `.env` |
| Реальна доставка слотів Telegram | не перевірена: ще немає користувацької підписки та реального знайденого слота |
| Автовідновлення контейнерів після старту ВМ | увімкнено `podman-restart.service`; саму ВМ потрібно запустити |

Offline перевірки охоплюють parsing, усі формати реальних адрес, service mapping,
fingerprint, state transitions, reappearance cooldown, overlapping subscriptions,
спільне polling та fan-out, групування дат/часу, pause/ownership, рестарт,
Telegram retry/ambiguous delivery, backoff/circuit і failure preservation.
Ті самі repository/outbox тести пройшли на PostgreSQL із окремою schema на тест.
Restart dedup тест закриває фізичні DB connections і повторно читає збережений
стан; повторної доставки SENT немає. Test target `verification` відокремлений
від production image. Acceptance БД не використовує production credentials.

Для ДП «Документ» знайдено й розгорнуто робочий режим звичайного Chromium
із Xvfb у Podman. Немає stealth, proxy rotation, перенесення clearance cookies
чи автоматичного проходження challenge. Реальні positive fixtures містять
три дати й шість часових вікон для водійських посвідчень у центрі «Готово»,
по 14 місць у кожному. Для паспортних послуг перевірені відповіді порожні.
Headless mode залишається непридатним для цього середовища.

Контейнерний deployment acceptance завершено, бот залишено запущеним.
Доступність провайдерів може змінюватися; HTTP 429 і challenge залишаються
явними помилками, а не «0 місць». У production не виконувалися бронювання,
введення персональних даних або авторизація. Реальна доставка знайденого слота
в користувацький Telegram чат усе ще залежить від активної підписки користувача.

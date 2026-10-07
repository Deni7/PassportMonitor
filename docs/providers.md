# Дослідження провайдерів — 2026-10-06

## ДМСУ

Офіційна сторінка: https://dmsu.gov.ua/services/online.html; застосунок:
https://cherga.dmsu.gov.ua/. Поточний frontend — Next.js. Перевірено публічний
chunk `65f3a5f745ecc142.js`. Усі наведені GET виконані без cookies, CSRF або
авторизації та повернули HTTP 200:

| Запит | Контракт |
|---|---|
| `/api/v1/departments/regions` | `{"data": [{"id": int, "label": str}]}` |
| `/api/v1/departments/23` | той самий envelope, label містить код, місто й адресу |
| `/api/v1/services/101` | той самий envelope; 1 — закордонний паспорт, 2 — ID |
| `/api/v1/days/101/1?startDate=2026-10-06&endDate=2026-10-31` | `{"data": []}`; frontend читає поле `date` елементів |
| `/api/v1/time/101/1?date=2026-10-07` | `{"data": []}`; frontend читає поле `time` елементів |

Додатково отримано каталоги всіх 22 регіонів: 365 підрозділів. Labels мають
різні формати (місто перед/після адреси, назва установи, селище/смт); вони
покриті тестом фактичного повного каталогу. Optional HTTP integration тест пройдено.

Позитивну відповідь зі слотами під час дослідження не отримано. Поля непорожніх
відповідей підтверджені кодом frontend, але потребують live contract перевірки.
Каталог централізований, endpoint-и не залежать від регіону. Окремого каталогу
міст немає: їх визначаємо з labels підрозділів; незрозумілі labels спричиняють
PARSING_ERROR, а не втрату підрозділу. Для вибраного регіону охоплюються всі
підрозділи; для міста — всі підрозділи з точним нормалізованим збігом міста.

Deep link не підтверджено: використовується перевірена загальна сторінка черги.
Запити customer/check, customer/book, orders і sms категорично не виконуються.
У досліджених GET challenge не спостерігався; це не гарантія його відсутності.

## ДП «Документ»

Офіційний каталог: https://pasport.org.ua/centers. У меню центрів на сторінці
https://ukraina.pasport.org.ua/solutions/e-queue присутні посилання на окремі
центри з містом та адресою. Джерело меню — JSON `items[]` у `x-data` з полями
`link`, `city`, `title`; перший блок містить українські центри. Львів:
https://lviv2.pasport.org.ua/solutions/e-queue.
Це реальні офіційні адреси, відкриті та перевірені в браузері.

Досліджено `app.main.7.37.5.js`, `app.m-queue-form.7.37.5.js` та
`app.m-queue-qlogickFormHaku.7.37.5.js`. У DOM `form#services` є `x-data` з
JSON-параметрами `url`, `center`, `csrf`. csrf — **назва** динамічного поля зі
значенням `1`. У Київському центрі center=15; каталог послуг у `select#service`:
4 — «Закордонний паспорт та (або) ID-картка», 2 — водійське посвідчення.
Не можна вважати service ID 4 універсальним для інших центрів.

Публічні read-only запити frontend:

* POST на URL поточної сторінки, multipart form: `form=days`,
  `ServiceCenterId`, `ServiceId`, `<csrf>=1`; frontend читає `days[]` з
  `datePart`, `date`, `allowedJobCount`.
* POST туди ж: `form=times`, ті ж поля та `Date`; frontend читає
  `timeSlots[]` з `startTime`, `slot`, `isAllowed`; тільки `isAllowed === true`.
* Session cookies підтримуються frontend через `withCredentials: true`.

Підтверджено реальними JSON 2026-10-06: `days[].isAllowed` фільтрує дати,
`timeSlots[].isAllowed` фільтрує час. У `slot` приходить public label
`09:00 — 14 вільних слотів`; відома кількість нормалізується в `Slot.count`.
Збережено позитивні відповіді для водійських посвідчень центру «Готово»:
2026-10-08/09/10 та шість часових вікон 2026-10-08.

Звичайний HTTP GET повертає 403. Чистий headless Chromium на Windows та Linux
отримував CLOUDFLARE. Звичайний Chromium із Xvfb у цій самій Linux ВМ успішно
отримав каталог, послуги, дати та час. Це типовий режим контейнера;
`DOCUMENT_BROWSER_HEADLESS=false`. Challenge не проходився й не обходився.
Каталог читається з офіційної загальної сторінки
`https://pasport.org.ua/solutions/e-queue`; сторінки центрів використовуються
для послуг і availability. Інтенсивні повторні перевірки центру «Україна»
отримали 429; backend враховує Retry-After і не робить негайних повторів.

Backend використовує довгоживучий Playwright Chromium/context, виключно вибір
послуги/дати та перехоплення JSON response. Дані зі слотами не scrape-яться з HTML.
HTML/DOM використовуються лише для вбудованого JSON каталогу центрів і options
послуг; окремий підтверджений JSON endpoint саме цього меню не знайдено.
CAPTCHA/challenge повертає спеціальну помилку; stealth,
proxy rotation, перенесення clearance cookies і обходи не застосовуються.
Інші варіанти form (BankID/Дія) повертають AUTH_REQUIRED, якщо read-only dates
не доступні; жодні персональні поля не заповнюються.

У першій версії охоплюються українські центри. Закордонні центри потребують
окремого timezone mapping, тому навмисно виключені з каталогу.

## Докази та повторна перевірка

Реальні анонімні JSON ДМСУ збережені у `tests/fixtures/dmsu/`.
JS/HTML артефакти дослідження зберігаються локально у `docs/discovery/` і
виключені з git (містять короткоживучі nonce). У document fixtures є лише
витяг публічного каталогу DOM, synthetic error cases та реальні captures у
`tests/fixtures/document/live/` з джерелами в README. Captures містять тільки
public availability fields, без cookies, CSRF, headers чи персональних даних.
Звичайні тести не звертаються до сайтів. `pytest -m integration` виконує тільки
read-only операції. Успішний порожній результат відрізняється від timeout,
403/429, browser error та зміни контракту.

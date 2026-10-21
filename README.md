# NetGuard Monitor — база даних і програмний продукт

Контекстна, логічна та фізична моделі бази даних системи моніторингу мережевого обладнання
та ДБЖ інтернет-провайдера **NetGuard Monitor** (продовження ЛР1–ЛР4 з дисципліни
«Технології створення програмних продуктів»), програмна логіка БД і програмний продукт на
Python, що з нею працює: сервіси опитування, аналізу аварій і сповіщень та веб-інтерфейс.

## Вміст репозиторію

| Шлях | Опис |
|---|---|
| `docs/ЛР1_Організація_БД_NetGuard.docx` | Повний звіт (для курсової): аналіз предметної області, контекстна, логічна та фізична моделі (повні схеми — на аркушах А3), зв'язок БД з YAML-конфігурацією SNMP, тестування, опис 15 таблиць, функціональні вимоги до програмної реалізації, DDL у додатку |
| `docs/ЛР1_Організація_БД_NetGuard.pdf` | Той самий повний звіт у PDF |
| `docs/ЛР1_Організація_БД_NetGuard_скорочено.docx` | Скорочений звіт для здачі ЛР: моделі БД, опис таблиць і розділ про програмний продукт (мета, вхідні й вихідні дані, системні вимоги, засоби реалізації, алгоритм і блок-схеми, інтерфейс, знімки екрана із запитами до БД) |
| `sql/schema.sql` | DDL фізичної моделі для PostgreSQL 15+ |
| `sql/lr2_logic.sql` | Програмна логіка БД: тригери, процедури, функції, представлення, ролі СУБД |
| `sql/seed_reference.sql` | Довідники, на які посилається YAML: ролі, вузли, метрики |
| `sql/tests_constraints.sql` | Автотест обмежень: позитивний сценарій + 51 негативна перевірка |
| `sql/tests_logic.sql` | Автотест програмної логіки: 44 перевірки тригерів, процедур, ролей і звітів |
| `config/snmp_config.yaml` | Конфігурація SNMP-опитування: облікові дані, профілі (метрика → OID), пристрої |
| `config/snmp_config.demo.yaml` | Демонстраційна конфігурація: 5 вузлів, 21 пристрій (6 ДБЖ) |
| `tools/snmp_config_sync.py` | Застосування YAML-файлу з командного рядка (та сама функція БД, що й у веб-інтерфейсі) |
| `tools/screenshots.py` | Знімки екрана веб-інтерфейсу для звіту (Playwright) |
| `app/netguard/` | Програмний продукт: сервіси й веб-інтерфейс (Python, NiceGUI, psycopg 3) |
| `docs/screenshots/` | Знімки екрана інтерфейсу та сторінки «Запити до БД» |
| `docs/diagrams/` | Діаграми моделей БД і блок-схеми алгоритмів; `src/*.dot` — джерела для Graphviz |

## Склад моделі

- **15 таблиць:** `roles`, `users`, `locations`, `equipment_type`, `metric`, `snmp_config`,
  `snmp_credential`, `snmp_oid_map`, `equipment`, `ups_details`, `threshold`, `metric_reading`,
  `incident`, `notification`, `event_log`;
- **12 типів ENUM**, 90 обмежень (15 PK, 23 FK, 14 UNIQUE, 38 CHECK) і 28 індексів
  (серед них BRIN, часткові та часткові унікальні — зокрема `uq_equipment_ip`: IP-адреса
  унікальна серед активних пристроїв);
- `metric_reading` секціоновано за місяцями (`PARTITION BY RANGE (read_at)`);
- нормалізація до НФБК; бізнес-правила зіставлено з механізмами, що їх забезпечують.

## Програмна логіка БД (`sql/lr2_logic.sql`)

| Об'єкт | Призначення |
|---|---|
| `reading_check_thresholds` (тригер на `metric_reading`) | показник поза порогом → інцидент без дублювання відкритих |
| `sp_register_poll_success`, `sp_register_poll_failure` | облік відповідей пристрою; після N невдач поспіль (`offline_after_failures` з YAML, типово 3) — статус offline та інцидент |
| `incident_validate`, `incident_events` (тригери) | допустимі переходи статусів, відповідність порогу типу обладнання, журнал подій, постановка сповіщень у чергу |
| `sp_acknowledge_incident`, `sp_resolve_incident`, `sp_close_incident` | дії інженера з інцидентом |
| `fn_escalate_overdue` | ескалація інцидентів, не прийнятих за 5 хв, адміністраторам |
| `fn_enqueue_notifications` | черга сповіщень з антифлуд-перевіркою (одне сповіщення на інцидент, отримувача й канал за 10 хв) |
| `sp_apply_snmp_config` | застосування YAML-конфігурації однією транзакцією |
| `fn_authenticate`, `fn_hash_password` | вхід користувача; bcrypt через `pgcrypto` |
| `fn_register_ups`, тригер `ups_details_category` | реєстрація ДБЖ однією дією; паспорт лише для категорії ups |
| тригери аудиту `equipment_audit`, `threshold_audit`, `users_audit` | журнал змін з автором і значеннями «було/стало» |
| `v_snmp_poll_plan`, `v_equipment_current_state`, `v_incident_list` | план опитування, поточний стан, список інцидентів |
| `fn_metric_history`, `fn_report_uptime`, `fn_report_incidents`, `fn_report_ups_battery` | графіки й звіти |
| `fn_create_partitions`, `fn_drop_old_partitions` | обслуговування секцій телеметрії |
| ролі `ng_web`, `ng_poller`, `ng_alerting`, `ng_notifier`, `ng_report` | найменші привілеї для кожного сервісу |

## Програмний продукт (`app/netguard`)

| Модуль | Запуск | Що робить |
|---|---|---|
| Polling Service | `python -m netguard.services.poller` | читає план опитування, опитує пристрої (емулятор SNMP-агентів), пише показники |
| Alerting Service | `python -m netguard.services.alerting` | ескалація прострочених інцидентів, журнал аварій |
| Notification Service | `python -m netguard.services.notifier` | доставляє сповіщення з черги (веб, e-mail, Telegram) |
| Веб-інтерфейс | `python -m netguard.web` | панель моніторингу, обладнання, інциденти, звіти, адміністрування, запити до БД |
| Усе разом | `python -m netguard` | запускає всі сервіси окремими процесами |
| Демо-дані | `python -m netguard.demo_setup --reset` | схема, логіка, довідники, користувачі, 2 версії YAML, історія за 7 діб |

Інтерфейс написано на Python бібліотекою NiceGUI: вона перетворює Python-код на веб-сторінки
(Vue/Quasar у браузері) і працює поверх FastAPI. Брокер повідомлень із ЛР2 ТСПП (RabbitMQ)
замінено механізмом `LISTEN/NOTIFY` PostgreSQL: БД сама будить сервіси, окремий брокер не потрібен.

### Запуск

```bash
cd app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export NETGUARD_DSN="host=localhost dbname=netguard user=postgres"   # власник схеми netguard
export NETGUARD_STORAGE_SECRET="довгий-випадковий-рядок"
python -m netguard.demo_setup --reset            # або розгортання БД вручну (нижче)
NETGUARD_SIM_FAULTS="SW-VYN-02=offline;UPS-TRO-01=battery_low" python -m netguard
# веб-інтерфейс: http://localhost:8080
```

Демонстраційні облікові записи: `admin` / `Admin#2026` (адміністратор),
`a.koval` / `Support#2026` (сапорт). Інші змінні середовища описано в `app/netguard/settings.py`;
без `SMTP_HOST` і `TELEGRAM_BOT_TOKEN` сповіщення e-mail і Telegram пишуться у `var/outbox.log`.
Сторінка «Запити до БД» перемикає ролі СУБД (`SET LOCAL ROLE`), тому обліковий запис
`NETGUARD_DSN` має бути власником схеми або членом ролей `ng_*`.

## Зв'язок БД з YAML-конфігурацією SNMP

`config/snmp_config.yaml` описує, **як** опитувати обладнання. Спершу файл суворо перевіряється
(`app/netguard/snmp_yaml.py`): дозволені лише відомі ключі, дублікати ключів, імен та IP-адрес
заборонені, числа й адреси мають правильний тип, рядки не довші за стовпці БД, секрети записано
тільки як `${VAR}`, для SNMPv3 обов'язковий `auth_protocol`. Помилка у файлі означає повідомлення
(і код виходу 2 у командному рядку); у БД нічого не потрапляє. Далі функція
`sp_apply_snmp_config` в одній транзакції:

- реєструє версію в `snmp_config` (контрольна сума SHA-256 і повний вміст у JSONB); чинною може
  бути лише одна версія. Той самий вміст, що вже активний, пропускається, а раніше застосований
  стає активним знову;
- синхронізує `snmp_credential` (секретів у БД немає, лише ім'я змінної середовища),
  `equipment_type` (за ключем профілю; профілі, яких більше немає у файлі, лишаються в БД)
  і `snmp_oid_map` (метрика → OID і множник);
- синхронізує `equipment` за `name`: пристрої, яких більше немає у файлі, деактивуються
  (їхні IP можна віддати новим пристроям), зміна IP оновлює наявний запис зі збереженням
  історії, а пристрої з файлу можуть обмінятися адресами. Облікові записи, яких немає у файлі,
  видаляються, лише якщо на них ніхто не посилається;
- пише подію `config_change` в `event_log`.

Невідомий вузол чи код метрики — зрозуміла помилка, а IP, зайнятий активним пристроєм, якого
немає у файлі, — порушення `uq_equipment_ip`. В обох випадках транзакція відкочується, і чинною
лишається попередня версія.

```bash
set -o pipefail   # щоб помилка завантажувача не губилася в конвеєрі
python3 tools/snmp_config_sync.py config/snmp_config.yaml | psql -d netguard -v ON_ERROR_STOP=1
```

## Розгортання БД вручну

```bash
createdb netguard
psql -d netguard -v ON_ERROR_STOP=1 -f sql/schema.sql             # 1. схема
psql -d netguard -v ON_ERROR_STOP=1 -f sql/lr2_logic.sql          # 2. програмна логіка й ролі
psql -d netguard -v ON_ERROR_STOP=1 -f sql/seed_reference.sql     # 3. довідники
set -o pipefail
python3 tools/snmp_config_sync.py config/snmp_config.yaml \
  | psql -d netguard -v ON_ERROR_STOP=1                           # 4. YAML-конфігурація SNMP
psql -d netguard -v ON_ERROR_STOP=1 -f sql/tests_constraints.sql  # 5. перевірка обмежень
psql -d netguard -v ON_ERROR_STOP=1 -f sql/tests_logic.sql        # 6. перевірка програмної логіки
```

`schema.sql` можна виконувати повторно: він видаляє і заново створює схему `netguard`.
`lr2_logic.sql` і `seed_reference.sql` теж можна запускати повторно. Обидва автотести працюють
у транзакції з відкатом, тож даних у БД не лишають; якщо хоч одна перевірка не пройшла, psql
завершується з помилкою. Усе перевірено на PostgreSQL 16 і Python 3.13.

## Перегенерація діаграм

```bash
for f in docs/diagrams/src/*.dot; do
  dot -Tpng -Gdpi=200 "$f" -o "docs/diagrams/$(basename "${f%.dot}").png"
done
for f in docs/diagrams/lab/src/*.dot; do
  dot -Tpng -Gdpi=200 "$f" -o "docs/diagrams/lab/$(basename "${f%.dot}").png"
done
```

# ЛР1 «Організація баз даних» — моделі БД NetGuard Monitor

Контекстна, логічна та фізична моделі бази даних системи моніторингу мережевого обладнання
та ДБЖ інтернет-провайдера **NetGuard Monitor** (продовження ЛР1–ЛР4 з дисципліни
«Технології створення програмних продуктів»).

## Вміст репозиторію

| Шлях | Опис |
|---|---|
| `docs/ЛР1_Організація_БД_NetGuard.docx` | Звіт (43 с.): аналіз предметної області, контекстна, логічна та фізична моделі, зв'язок БД з YAML-конфігурацією SNMP, опис 15 таблиць з типами даних, висновки з функціональними вимогами до ЛР2, DDL у додатку |
| `docs/ЛР1_Організація_БД_NetGuard.pdf` | Той самий звіт у PDF |
| `sql/schema.sql` | DDL фізичної моделі для PostgreSQL 15+ |
| `config/snmp_config.yaml` | Конфігурація SNMP-опитування: облікові дані, профілі (метрика → OID), пристрої |
| `tools/snmp_config_sync.py` | Прототип завантажувача YAML → БД (одна транзакція, контроль версій за SHA-256) |
| `docs/diagrams/*.png` | Діаграми: контекстна, ER (нотація Чена), логічна (crow's foot), фізична (3 частини) |
| `docs/diagrams/src/*.dot` | Джерела діаграм для Graphviz |

## Склад моделі

- **15 таблиць:** `roles`, `users`, `locations`, `equipment_type`, `metric`, `snmp_config`,
  `snmp_credential`, `snmp_oid_map`, `equipment`, `ups_details`, `threshold`, `metric_reading`,
  `incident`, `notification`, `event_log`;
- **12 типів ENUM**, 91 обмеження (15 PK, 23 FK, 15 UNIQUE, 38 CHECK) і 27 індексів
  (серед них BRIN і часткові);
- `metric_reading` секціоновано за місяцями (`PARTITION BY RANGE (read_at)`);
- нормалізація до НФБК; 18 бізнес-правил, кожне зіставлене з механізмом, що його забезпечує.

## Зв'язок БД з YAML-конфігурацією SNMP

`config/snmp_config.yaml` описує, **як** опитувати обладнання. Кожне застосування зміненого файлу:

- реєструє нову версію в `snmp_config` (контрольна сума SHA-256 і повний вміст у JSONB);
  чинною може бути лише одна версія;
- синхронізує `snmp_credential` (секретів у БД немає, лише ім'я змінної середовища з `${VAR}`),
  `equipment_type` (за ключем профілю), `snmp_oid_map` (метрика → OID і множник)
  та `equipment` (за IP; пристрої, яких більше немає у файлі, деактивуються);
- пише подію `config_change` в `event_log`.

Невідомий вузол, профіль, метрика чи обліковий запис порушує NOT NULL або FK, і транзакція
відкочується: попередня версія лишається чинною.

```bash
python3 tools/snmp_config_sync.py config/snmp_config.yaml | psql -d netguard
```

Профілі в YAML посилаються на коди метрик із довідника `metric`, а пристрої — на вузли з
`locations`, тому ці довідники мають бути заповнені заздалегідь.

## Розгортання схеми

```bash
createdb netguard
psql -d netguard -v ON_ERROR_STOP=1 -f sql/schema.sql
```

Скрипт можна виконувати повторно: він видаляє і заново створює схему `netguard`.
Перевірено на PostgreSQL 16.

## Перегенерація діаграм

```bash
for f in docs/diagrams/src/*.dot; do
  dot -Tpng -Gdpi=200 "$f" -o "docs/diagrams/$(basename "${f%.dot}").png"
done
```

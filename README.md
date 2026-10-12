# ЛР1 «Організація баз даних» — моделі БД NetGuard Monitor

Контекстна, логічна та фізична моделі бази даних системи моніторингу мережевого обладнання
та ДБЖ інтернет-провайдера **NetGuard Monitor** (продовження ЛР1–ЛР4 з дисципліни
«Технології створення програмних продуктів»).

## Вміст репозиторію

| Шлях | Опис |
|---|---|
| `docs/ЛР1_Організація_БД_NetGuard.docx` | Звіт (31 с.): аналіз предметної області, контекстна, логічна та фізична моделі, опис 12 таблиць з типами даних, висновки з функціональними вимогами до ЛР2, DDL у додатку |
| `docs/ЛР1_Організація_БД_NetGuard.pdf` | Той самий звіт у PDF |
| `sql/schema.sql` | DDL фізичної моделі для PostgreSQL 15+ |
| `docs/diagrams/*.png` | Діаграми: контекстна, ER (нотація Чена), логічна (crow's foot), фізична (2 частини) |
| `docs/diagrams/src/*.dot` | Джерела діаграм для Graphviz |

## Склад моделі

- **12 таблиць:** `roles`, `users`, `locations`, `equipment_type`, `metric`, `equipment`,
  `ups_details`, `threshold`, `metric_reading`, `incident`, `notification`, `event_log`;
- **10 типів ENUM**, 66 обмежень (12 PK, 16 FK, 12 UNIQUE, 26 CHECK) і 20 індексів
  (серед них BRIN і часткові);
- `metric_reading` секціоновано за місяцями (`PARTITION BY RANGE (read_at)`);
- нормалізація до НФБК; 14 бізнес-правил, кожне зіставлене з механізмом, що його забезпечує.

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

"""Запити до БД (сторінка адміністратора «Запити до БД»): каталог демонстраційних SQL-запитів,
їх виконання та довільний запит лише для читання.

Запити вибірки виконуються від імені ролі ng_report у транзакції лише для читання;
запити, що змінюють дані (тригери, процедури, порушення обмежень), — у транзакції,
яка завжди відкочується, тож стан БД не змінюється. Помилка СУБД теж є результатом запиту:
її текст, SQLSTATE, обмеження й таблиця повертаються у відповіді 200, а не як помилка HTTP.
"""
import math
import time
from decimal import Decimal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import decimal_encoder, jsonable_encoder
from psycopg import postgres
from psycopg.rows import tuple_row
from psycopg.types.range import Range
from pydantic import BaseModel, Field

from ...auth import Principal
from ..deps import admin_user, db

router = APIRouter(prefix="/api/admin/queries", tags=["queries"])

SELECT, LOGIC, ERROR = "select", "logic", "error"
GROUPS = {SELECT: "Вибірка даних", "report": "Агрегування та звіти", LOGIC: "Програмна логіка (з відкатом)",
          ERROR: "Обмеження цілісності (очікувана помилка)"}

QUERIES = [
    dict(key="state", group=SELECT, kind=SELECT, title="Поточний стан обладнання ЦОД (представлення)",
         note="Представлення v_equipment_current_state бере останнє значення кожної метрики через LATERAL … LIMIT 1 "
              "по первинному ключу секціонованої таблиці metric_reading.",
         sql="""SELECT name AS "Пристрій", category AS "Категорія", status AS "Стан",
       last_values ->> 'battery_charge' AS "Заряд АКБ, %",
       last_values ->> 'input_voltage'  AS "Вхідна напруга, В",
       last_values ->> 'cpu_load'       AS "CPU, %",
       last_values ->> 'temperature'    AS "Температура, °C",
       open_incidents                   AS "Відкритих інцидентів"
  FROM v_equipment_current_state
 WHERE location = 'ЦОД'
 ORDER BY category, name;"""),
    dict(key="plan", group=SELECT, kind=SELECT, title="План опитування з YAML-конфігурації",
         note="Представлення v_snmp_poll_plan, з якого Polling Service бере адресу, облікові дані й OID кожної метрики. "
              "Секрет у БД не зберігається — лише ім'я змінної середовища.",
         sql="""SELECT equipment_name AS "Пристрій", ip AS "IP", snmp_version AS "SNMP",
       username AS "Користувач v3", secret_ref AS "Змінна з секретом",
       metric_code AS "Метрика", oid AS "OID", scale AS "Множник"
  FROM v_snmp_poll_plan
 WHERE equipment_name = 'UPS-DC-01'
 ORDER BY metric_id;"""),
    dict(key="open", group=SELECT, kind=SELECT, title="Відкриті інциденти з порогами та відповідальними (JOIN 6 таблиць)",
         note="Інциденти в статусах new, escalated, acknowledged з даними пристрою, вузла, порогу й користувача.",
         sql="""SELECT i.id AS "№", e.name AS "Пристрій", l.name AS "Вузол",
       coalesce(m.name, 'недоступність') AS "Метрика", i.metric_value AS "Значення",
       t.min_value AS "Мін.", t.max_value AS "Макс.", t.severity AS "Рівень",
       i.status AS "Статус", u.full_name AS "Прийняв", i.opened_at AS "Відкрито"
  FROM incident i
  JOIN equipment e   ON e.id = i.equipment_id
  JOIN locations l   ON l.id = e.location_id
  LEFT JOIN threshold t ON t.id = i.threshold_id
  LEFT JOIN metric m    ON m.id = t.metric_id
  LEFT JOIN users u     ON u.id = i.acknowledged_by
 WHERE i.status IN ('new', 'escalated', 'acknowledged')
 ORDER BY i.opened_at DESC;"""),
    dict(key="history", group=SELECT, kind=SELECT, title="Історія заряду АКБ під час аварії живлення (агрегування)",
         note="Функція fn_metric_history групує показники за інтервалами (date_bin): видно розряд АКБ ДБЖ вузла "
              "Теремки під час відключення мережі та подальший заряд.",
         sql="""SELECT bucket AS "Інтервал", round(avg_value::numeric, 1) AS "Середнє, %",
       min_value AS "Мін., %", max_value AS "Макс., %"
  FROM fn_metric_history(
         (SELECT id FROM equipment WHERE name = 'UPS-TER-01'), 'battery_charge',
         date_trunc('day', now()) - INTERVAL '3 days' + INTERVAL '1 hour',
         date_trunc('day', now()) - INTERVAL '3 days' + INTERVAL '9 hours',
         INTERVAL '30 minutes');"""),
    dict(key="lateral", group=SELECT, kind=SELECT, title="Останнє значення кожної метрики ДБЖ (LATERAL)",
         note="Для кожної метрики шаблону опитування — останній показник; запит використовує індекс первинного ключа "
              "і відсікання секцій.",
         sql="""SELECT m.name AS "Метрика", r.value AS "Значення", m.unit AS "Од.", r.read_at AS "Час"
  FROM equipment e
  JOIN snmp_oid_map om ON om.equipment_type_id = e.type_id
  JOIN metric m        ON m.id = om.metric_id
  CROSS JOIN LATERAL (SELECT value, read_at FROM metric_reading
                       WHERE equipment_id = e.id AND metric_id = m.id
                       ORDER BY read_at DESC LIMIT 1) r
 WHERE e.name = 'UPS-TER-01'
 ORDER BY m.id;"""),
    dict(key="mtta", group="report", kind=SELECT, title="Інциденти, MTTA і MTTR за вузлами (fn_report_incidents)",
         note="MTTA — середній час до прийняття інциденту, MTTR — середній час до усунення.",
         sql="""SELECT location AS "Вузол", category AS "Категорія", incidents AS "Інцидентів",
       critical AS "Критичних", offline AS "Недоступність", still_open AS "Відкриті",
       avg_mtta AS "Сер. MTTA", avg_mttr AS "Сер. MTTR"
  FROM fn_report_incidents(now() - INTERVAL '7 days', now());"""),
    dict(key="uptime", group="report", kind=SELECT, title="Доступність обладнання за 7 діб (fn_report_uptime)",
         note="Простій рахується як тривалість інцидентів недоступності, що перетинаються з періодом.",
         sql="""SELECT equipment AS "Пристрій", location AS "Вузол", downtime AS "Простій",
       uptime_pct AS "Доступність, %"
  FROM fn_report_uptime(now() - INTERVAL '7 days', now())
 ORDER BY uptime_pct, equipment
 LIMIT 8;"""),
    dict(key="ups", group="report", kind=SELECT, title="Стан АКБ ДБЖ за 7 діб (fn_report_ups_battery)",
         note="Поточний і мінімальний заряд, середня температура, автономність і вік батарей.",
         sql="""SELECT equipment AS "ДБЖ", location AS "Вузол", battery_age_days AS "Вік АКБ, діб",
       charge_last AS "Заряд, %", charge_min AS "Мін. заряд, %",
       temp_avg AS "Сер. t, °C", runtime_last_min AS "Автономність, хв"
  FROM fn_report_ups_battery(now() - INTERVAL '7 days', now());"""),
    dict(key="partitions", group="report", kind=SELECT, title="Секції таблиці metric_reading і кількість рядків",
         note="Таблицю показників секціоновано за місяцями (PARTITION BY RANGE); системний каталог pg_inherits "
              "містить секції, а tableoid показує, у якій секції лежить кожен рядок.",
         sql="""SELECT c.relname AS "Секція",
       pg_get_expr(c.relpartbound, c.oid) AS "Межі секції",
       coalesce(r.n, 0) AS "Рядків"
  FROM pg_inherits i
  JOIN pg_class c ON c.oid = i.inhrelid
  LEFT JOIN (SELECT tableoid, count(*) AS n
               FROM metric_reading GROUP BY tableoid) r ON r.tableoid = c.oid
 WHERE i.inhparent = 'metric_reading'::regclass
 ORDER BY c.relname
 LIMIT 8;"""),
    dict(key="events", group="report", kind=SELECT, title="Журнал подій за 7 діб за типами (GROUP BY, FILTER)",
         note="Агрегування журналу подій для звіту про активність.",
         sql="""SELECT event_type AS "Тип події", count(*) AS "Усього",
       count(*) FILTER (WHERE user_id IS NOT NULL) AS "Від імені користувачів",
       min(created_at) AS "Перша", max(created_at) AS "Остання"
  FROM event_log
 WHERE created_at > now() - INTERVAL '7 days'
 GROUP BY 1
 ORDER BY 2 DESC;"""),
    dict(key="trigger", group=LOGIC, kind=LOGIC, role="ng_poller", title="Тригер: показник поза порогом відкриває інциденти",
         note="Запис показника температури 72,5 °C (поріг попередження 60, критичний 70). Тригер reading_check_thresholds "
              "відкриває два інциденти, а тригер incident_events ставить сповіщення в чергу. Транзакцію відкочено.",
         sql="""INSERT INTO metric_reading (equipment_id, metric_id, read_at, value)
SELECT e.id, m.id, now(), 72.5
  FROM equipment e, metric m
 WHERE e.name = 'SW-DC-ACC-01' AND m.code = 'temperature';

SELECT i.id AS "Інцидент", i.status AS "Статус", i.metric_value AS "Значення",
       t.severity AS "Рівень порогу", t.max_value AS "Поріг",
       (SELECT count(*) FROM notification n WHERE n.incident_id = i.id) AS "Сповіщень у черзі"
  FROM incident i JOIN threshold t ON t.id = i.threshold_id
 WHERE i.equipment_id = (SELECT id FROM equipment WHERE name = 'SW-DC-ACC-01')
   AND i.opened_at = now();"""),
    dict(key="offline", group=LOGIC, kind=LOGIC, role="ng_poller", title="Процедура: три невдалі опитування поспіль → офлайн",
         note="sp_register_poll_failure збільшує лічильник; після третьої невдачі БД позначає пристрій offline "
              "і відкриває інцидент недоступності (SR-04.2). Транзакцію відкочено.",
         sql="""DO $$
DECLARE
    eq INTEGER := (SELECT id FROM equipment WHERE name = 'SW-PEC-01');
BEGIN
    FOR attempt IN 1..3 LOOP              -- пристрій тричі поспіль не відповів
        CALL sp_register_poll_failure(eq);
    END LOOP;
END $$;

SELECT e.name AS "Пристрій", e.status AS "Стан", e.failed_polls AS "Невдач поспіль",
       i.id AS "Інцидент", i.incident_type AS "Тип", i.status AS "Статус інциденту"
  FROM equipment e
  LEFT JOIN incident i ON i.equipment_id = e.id AND i.opened_at = now()
 WHERE e.name = 'SW-PEC-01';"""),
    dict(key="ack", group=LOGIC, kind=LOGIC, title="Процедура: прийняття інциденту в роботу",
         note="sp_acknowledge_incident блокує рядок інциденту, перевіряє статус і користувача, фіксує час і "
              "відповідального; тригер записує перехід у журнал подій. Транзакцію відкочено.",
         sql="""DO $$
DECLARE
    inc  BIGINT  := (SELECT id FROM incident WHERE status IN ('new', 'escalated')
                     ORDER BY opened_at LIMIT 1);
    engineer INTEGER := (SELECT id FROM users WHERE login = 'a.koval');
BEGIN
    CALL sp_acknowledge_incident(inc, engineer);
END $$;

SELECT i.id AS "№", i.status AS "Статус", i.acknowledged_at AS "Прийнято",
       u.full_name AS "Відповідальний", ev.message AS "Запис у журналі"
  FROM incident i
  JOIN users u ON u.id = i.acknowledged_by
  JOIN event_log ev ON ev.incident_id = i.id AND ev.created_at = now()
 WHERE i.acknowledged_at = now();"""),
    dict(key="auth", group=LOGIC, kind=LOGIC, title="Автентифікація: bcrypt-хеш через pgcrypto",
         note="fn_authenticate порівнює пароль із bcrypt-хешем (crypt), відмовляє заблокованим і фіксує спробу "
              "в журналі подій. Транзакцію відкочено.",
         sql="""SELECT * FROM fn_authenticate('a.koval', 'Support#2026');

SELECT login AS "Логін", left(password_hash, 29) || '…' AS "Хеш пароля (bcrypt)",
       is_active AS "Активний", last_login_at AS "Останній вхід"
  FROM users
 WHERE login IN ('a.koval', 'i.tkachenko');"""),
    dict(key="yaml", group=LOGIC, kind=LOGIC, title="Повторне застосування активної YAML-версії",
         note="sp_apply_snmp_config за контрольною сумою SHA-256 бачить, що версія вже активна, і нічого не змінює.",
         sql="""SELECT id AS "Версія", left(sha256, 16) || '…' AS "SHA-256",
       sp_apply_snmp_config(file_path, sha256, content) AS "Результат"
  FROM snmp_config
 WHERE is_active;"""),
    dict(key="check", group=ERROR, kind=ERROR, title="CHECK: інтервал опитування поза межами 10–60 с",
         note="Обмеження ck_equipment_interval (FR-04: не рідше ніж раз на 60 с).",
         sql="""UPDATE equipment SET poll_interval_s = 5
 WHERE name = 'SW-HOL-01';"""),
    dict(key="unique", group=ERROR, kind=ERROR, title="Частковий унікальний індекс: IP активного пристрою",
         note="uq_equipment_ip — IP-адреса унікальна серед активних пристроїв.",
         sql="""INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id)
SELECT type_id, location_id, 'SW-TEST-01', ip_address, credential_id
  FROM equipment
 WHERE name = 'SW-HOL-01';"""),
    dict(key="fk", group=ERROR, kind=ERROR, title="FK RESTRICT: видалення вузла, де встановлено обладнання",
         note="fk_equipment_location з правилом ON DELETE RESTRICT.",
         sql="""DELETE FROM locations WHERE name = 'Вузол Голосіїв';"""),
    dict(key="transition", group=ERROR, kind=ERROR, title="Тригер: недопустимий перехід статусу інциденту",
         note="Тригер incident_validate дозволяє лише переходи діаграми станів (new → escalated → acknowledged → "
              "resolved → closed).",
         sql="""UPDATE incident
   SET status = 'closed', acknowledged_at = now(), acknowledged_by = 1,
       resolved_at = now(), closed_at = now(), close_comment = 'без розгляду'
 WHERE id = (SELECT id FROM incident WHERE status IN ('new', 'escalated') LIMIT 1);"""),
    dict(key="role", group=ERROR, kind=ERROR, title="Розмежування прав: роль звітів не змінює дані",
         note="Роль СУБД ng_report має лише право читання.",
         sql="""SET LOCAL ROLE ng_report;
UPDATE threshold SET max_value = 99 WHERE id = 1;"""),
]
BY_KEY = {q["key"]: q for q in QUERIES}
MAX_ROWS = 500
CELL = {bytes: lambda b: "\\x" + b.hex(), Range: str,        # bytea і діапазони — текстом
        float: lambda f: f if math.isfinite(f) else str(f),  # NaN і нескінченності в JSON немає
        Decimal: lambda d: decimal_encoder(d) if d.is_finite() else str(d)}


class SqlIn(BaseModel):
    sql: str = Field(max_length=20000)


def _type_name(oid: int) -> str | None:
    """Ім'я типу стовпця без модифікаторів (масив — з «[]»): за ним сторінка форматує INTERVAL."""
    t = postgres.types.get(oid)
    if t is None:                                # enum та інші типи, створені в БД
        return None
    return t.name + "[]" if oid == t.array_oid else t.name


def _error(e: psycopg.Error) -> dict:
    d = e.diag
    return {"message": d.message_primary or str(e), "detail": d.message_detail, "sqlstate": d.sqlstate,
            "constraint": d.constraint_name, "table": d.table_name}


async def execute(sql: str, kind: str, role: str | None = None, single: bool = False) -> dict:
    """Виконує текст (можливо, кілька операторів) і відкочує транзакцію; повертає колонки, їхні типи й рядки
    останнього набору (INTERVAL — у секундах), час у мс або помилку. single — рівно один оператор (розширений
    протокол): так довільний запит не завершить транзакцію лише для читання командою COMMIT."""
    async with db.pool.connection() as conn:
        t0 = time.perf_counter()
        cols, types, rows, err = None, [], [], None
        try:
            async with conn.transaction(force_rollback=True):
                await conn.execute("SET LOCAL statement_timeout = '10s'")
                if kind == SELECT:
                    await conn.execute("SET TRANSACTION READ ONLY")
                    await conn.execute("SET LOCAL ROLE ng_report")
                elif role:                       # від імені сервісу, що виконує цю дію
                    await conn.execute(f"SET LOCAL ROLE {role}")
                cur = conn.cursor(row_factory=tuple_row)
                if single:
                    async with conn.pipeline():
                        await cur.execute(sql)
                else:
                    await cur.execute(sql)
                while True:
                    if cur.description:
                        cols = [d.name for d in cur.description]
                        types = [_type_name(d.type_code) for d in cur.description]
                        rows = await cur.fetchmany(MAX_ROWS)
                    if not cur.nextset():
                        break
        except psycopg.Error as e:
            cols, types, rows, err = None, [], [], _error(e)
        ms = (time.perf_counter() - t0) * 1000
    return {"columns": cols, "types": types, "rows": jsonable_encoder(rows, custom_encoder=CELL), "ms": ms,
            "error": err}


@router.get("")
async def catalogue(_user: Principal = Depends(admin_user)):
    """Групи й демонстраційні запити з поясненнями та текстом SQL."""
    return {"groups": GROUPS, "queries": QUERIES}


@router.post("/sql")
async def free_sql(body: SqlIn, _user: Principal = Depends(admin_user)):
    """Довільний запит: один оператор від імені ролі ng_report, лише читання, тайм-аут 10 с, перші 500 рядків."""
    return await execute(body.sql, SELECT, single=True)


@router.post("/{key}/run")
async def run(key: str, _user: Principal = Depends(admin_user)):
    """Демонстраційний запит із каталогу; очікувана помилка СУБД — звичайний результат з описом помилки."""
    q = BY_KEY.get(key)
    if not q:
        raise HTTPException(404, "Запит не знайдено")
    return await execute(q["sql"], q["kind"], q.get("role"))

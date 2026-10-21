"""Сторінка «Запити до БД»: демонстрація роботи бази даних SQL-запитами з результатами.

Запити вибірки виконуються від імені ролі ng_report у транзакції лише для читання;
запити, що змінюють дані (тригери, процедури, порушення обмежень), — у транзакції,
яка завжди відкочується, тож стан БД не змінюється.
"""
import time

import psycopg
from nicegui import ui

from .common import admin_only, db, frame, jsonable

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


async def execute(sql: str, kind: str, role: str | None = None):
    """Виконує текст (можливо, кілька операторів); повертає (колонки, рядки, мс) останнього набору або помилку."""
    async with db.pool.connection() as conn:
        t0 = time.perf_counter()
        try:
            async with conn.transaction(force_rollback=True):
                await conn.execute("SET LOCAL statement_timeout = '10s'")
                if kind == SELECT:
                    await conn.execute("SET TRANSACTION READ ONLY")
                    await conn.execute("SET LOCAL ROLE ng_report")
                elif role:                       # від імені сервісу, що виконує цю дію
                    await conn.execute(f"SET LOCAL ROLE {role}")
                cur = conn.cursor()
                await cur.execute(sql)
                cols, rows = None, []
                while True:
                    if cur.description:
                        cols = [d.name for d in cur.description]
                        rows = await cur.fetchmany(500)
                    if not cur.nextset():
                        break
            return cols, rows, (time.perf_counter() - t0) * 1000, None
        except psycopg.Error as e:
            return None, [], (time.perf_counter() - t0) * 1000, e


def show_result(cols, rows, ms, err, kind):
    if err is not None:
        d = err.diag
        expected = kind == ERROR
        with ui.card().classes("w-full p-4 " + ("bg-orange-1" if expected else "bg-red-1")):
            with ui.row().classes("items-center gap-2"):
                ui.icon("gpp_good" if expected else "error").classes("text-2xl " + ("text-deep-orange" if expected
                                                                                     else "text-negative"))
                ui.label("Очікувана помилка: СУБД відхилила операцію" if expected else "Помилка виконання") \
                    .classes("font-semibold")
            ui.label(f"ERROR: {d.message_primary}").classes("font-mono text-sm mt-1")
            if d.message_detail:
                ui.label(f"DETAIL: {d.message_detail}").classes("font-mono text-sm")
            meta = [f"SQLSTATE {d.sqlstate}"] + ([f"обмеження {d.constraint_name}"] if d.constraint_name else []) + \
                   ([f"таблиця {d.table_name}"] if d.table_name else [])
            ui.label(" · ".join(meta)).classes("text-xs text-grey-8 mt-1")
            ui.label(f"Транзакцію відкочено · {ms:.0f} мс").classes("text-xs text-grey-7")
        return
    if cols is None:
        ui.label(f"Виконано без набору результатів · {ms:.0f} мс").classes("text-sm text-grey-7")
        return
    data = [{"_k": i, **{str(j): jsonable(v) for j, v in enumerate(r.values())}} for i, r in enumerate(rows)]
    for row in data:
        for k, v in row.items():
            if v is None:
                row[k] = "NULL"
            elif isinstance(v, float):
                row[k] = round(v, 3)
    ui.table(columns=[{"name": str(j), "label": c, "field": str(j), "align": "left"} for j, c in enumerate(cols)],
             rows=data, row_key="_k").classes("w-full").props("dense flat bordered :pagination='{rowsPerPage: 0}'"
                                                              " hide-pagination")
    tail = " · транзакцію відкочено, дані не змінено" if kind == LOGIC else ""
    ui.label(f"Рядків: {len(rows)}{' (показано перші 500)' if len(rows) == 500 else ''} · {ms:.0f} мс{tail}") \
        .classes("text-xs text-grey-7")


@ui.page("/queries")
async def queries_page(q: str = "state", run: bool = False):
    await frame("Запити до БД", "Демонстрація роботи бази даних: SQL-запит і результат його виконання в PostgreSQL")
    if not admin_only():
        return
    current = {"q": BY_KEY.get(q, QUERIES[0])}

    @ui.refreshable
    async def panel(execute_now: bool = False):
        cq = current["q"]
        with ui.card().classes("w-full p-5 gap-3").props("id=query-panel"):
            with ui.row().classes("items-center gap-2"):
                ui.badge(GROUPS[cq["group"]], color="secondary")
                ui.label(cq["title"]).classes("text-lg font-semibold")
            role = "ng_report, лише читання" if cq["kind"] == SELECT else cq.get("role") or "ng_web"
            ui.label(f"Роль СУБД: {role}").classes("text-xs text-grey-7")
            ui.label(cq["note"]).classes("text-sm text-grey-8")
            ui.code(cq["sql"], language="postgresql").classes("w-full text-[13px]")
            result = ui.column().classes("w-full gap-2")

            async def go():
                result.clear()
                with result:
                    show_result(*(await execute(cq["sql"], cq["kind"], cq.get("role"))), cq["kind"])

            ui.button("Виконати", icon="play_arrow", on_click=go).props("unelevated")
        if execute_now:
            await go()

    with ui.tabs().classes("w-full") as tabs:
        t_demo = ui.tab("Демонстраційні запити")
        t_free = ui.tab("Довільний запит (лише читання)")
    with ui.tab_panels(tabs, value=t_demo).classes("w-full bg-transparent"):
        with ui.tab_panel(t_demo).classes("p-0"):
            with ui.row().classes("w-full gap-4 items-start no-wrap"):
                with ui.card().classes("w-80 p-0 shrink-0"):
                    for gkey, gtitle in GROUPS.items():
                        with ui.expansion(gtitle, value=True).classes("w-full").props("dense header-class=text-primary"):
                            for item in [x for x in QUERIES if x["group"] == gkey]:
                                async def pick(item=item):
                                    current["q"] = item
                                    await panel.refresh(execute_now=False)
                                ui.item(item["title"], on_click=pick).props("clickable dense").classes("text-sm")
                with ui.column().classes("flex-1 min-w-0"):
                    await panel(execute_now=run)
        with ui.tab_panel(t_free).classes("p-0"):
            with ui.card().classes("w-full p-5 gap-3"):
                ui.label("Запит виконується від імені ролі ng_report у транзакції лише для читання; "
                         "тайм-аут — 10 с, показуються перші 500 рядків.").classes("text-sm text-grey-8")
                sql = ui.textarea(value="SELECT name, status, last_seen_at\n  FROM equipment\n ORDER BY last_seen_at DESC\n LIMIT 10;") \
                    .props("outlined autogrow").classes("w-full font-mono")
                out = ui.column().classes("w-full gap-2")

                async def free():
                    out.clear()
                    with out:
                        show_result(*(await execute(sql.value, SELECT)), SELECT)

                ui.button("Виконати", icon="play_arrow", on_click=free).props("unelevated")

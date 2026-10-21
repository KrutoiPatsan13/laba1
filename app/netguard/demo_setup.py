"""Розгортання демонстраційної БД NetGuard Monitor.

    python -m netguard.demo_setup --reset      # схема + логіка + довідники + демо-дані

Кроки: sql/schema.sql → sql/lr2_logic.sql → sql/seed_reference.sql → вузли й користувачі →
YAML-конфігурація (config/snmp_config.yaml, потім config/snmp_config.demo.yaml — дві версії)
→ паспорти ДБЖ і пороги → історія показників за N діб (формули емулятора SNMP-агентів)
з аварійними сценаріями, інцидентами в різних статусах, сповіщеннями та журналом подій.
Підключення — NETGUARD_DSN від імені власника схеми (суперкористувача): історичні дані
завантажуються з вимкненими перевірками життєвого циклу (netguard.bypass_checks).
"""
import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .services.emulator import UPTIME_ANCHOR, baseline
from .settings import REPO_DIR, settings
from .snmp_yaml import load, to_json

KYIV = ZoneInfo("Europe/Kyiv")
STEP = timedelta(minutes=5)

LOCATIONS = [
    ("Вузол Троєщина", "access_node", "вул. Закревського, 47", "Районний вузол доступу, шафа на 1-му поверсі"),
    ("Вузол Позняки", "access_node", "вул. Драгоманова, 15", "Районний вузол доступу, підвальне приміщення"),
    ("Вузол Виноградар", "access_node", "просп. Правди, 66", "Районний вузол доступу"),
]

# login, ПІБ, роль, пароль, e-mail, Telegram chat id, активний
USERS = [
    ("admin", "Петренко Олег Іванович", "admin", "Admin#2026", "o.petrenko@netguard.example", 400100101, True),
    ("s.lysenko", "Лисенко Сергій Миколайович", "admin", "Admin#2026", "s.lysenko@netguard.example", None, True),
    ("a.koval", "Коваль Андрій Сергійович", "support", "Support#2026", "a.koval@netguard.example", 400100201, True),
    ("m.shevchuk", "Шевчук Марина Олегівна", "support", "Support#2026", "m.shevchuk@netguard.example", 400100202, True),
    ("d.bondar", "Бондар Дмитро Петрович", "support", "Support#2026", None, 400100203, True),
    ("i.tkachenko", "Ткаченко Ірина Василівна", "support", "Support#2026", "i.tkachenko@netguard.example", None, False),
]

# профіль, метрика, рівень, min, max
THRESHOLDS = [
    ("apc-symmetra-px", "battery_charge", "warning", 50, None),
    ("apc-symmetra-px", "battery_charge", "critical", 30, None),
    ("apc-symmetra-px", "battery_temperature", "warning", None, 40),
    ("apc-symmetra-px", "battery_temperature", "critical", None, 45),
    ("apc-symmetra-px", "battery_runtime", "warning", 900, None),
    ("apc-symmetra-px", "battery_runtime", "critical", 600, None),
    ("apc-symmetra-px", "input_voltage", "warning", 200, 245),
    ("apc-symmetra-px", "input_voltage", "critical", 190, 255),
    ("apc-symmetra-px", "output_load", "warning", None, 80),
    ("apc-symmetra-px", "output_load", "critical", None, 90),
    ("cisco-catalyst", "temperature", "warning", None, 60),
    ("cisco-catalyst", "temperature", "critical", None, 70),
    ("cisco-catalyst", "cpu_load", "warning", None, 80),
    ("cisco-catalyst", "cpu_load", "critical", None, 95),
    ("mikrotik-ccr", "temperature", "warning", None, 65),
    ("mikrotik-ccr", "temperature", "critical", None, 75),
    ("mikrotik-ccr", "cpu_load", "warning", None, 80),
    ("mikrotik-ccr", "cpu_load", "critical", None, 95),
]

# паспорти ДБЖ: модель, серійний номер, ВА, Вт, А·год, кількість модулів, напруга, дата встановлення АКБ
UPS = {
    "UPS-DC-01": ("Symmetra PX 40kW", "PX40-DC-0101", 40000, 40000, 9.0, 40, 192.0, "2024-03-12"),
    "UPS-DC-02": ("Symmetra PX 40kW", "PX40-DC-0102", 40000, 40000, 9.0, 40, 192.0, "2024-03-12"),
    "UPS-OBL-01": ("Symmetra PX 20kW", "PX20-OB-0201", 20000, 20000, 9.0, 20, 192.0, "2023-06-01"),
    "UPS-TRO-01": ("Symmetra PX 20kW", "PX20-TR-0301", 20000, 20000, 9.0, 20, 192.0, "2021-11-20"),
    "UPS-POZ-01": ("Symmetra PX 20kW", "PX20-PZ-0401", 20000, 20000, 9.0, 20, 192.0, "2022-09-05"),
    "UPS-VYN-01": ("Symmetra PX 20kW", "PX20-VY-0501", 20000, 20000, 9.0, 20, 192.0, "2025-02-17"),
}


def local(days_ago: int, hhmm: str, now: datetime) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = now.astimezone(KYIV).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=KYIV).astimezone(timezone.utc)


def scenarios(now: datetime) -> list[dict]:
    """Аварійні сценарії історії. fn(t, base) — значення метрики на t-й хвилині аварії;
    incidents — інциденти сценарію: рівень порогу (None — offline), хвилина виявлення від
    початку аварії, хто й коли (хв від відкриття) прийняв, усунув, закрив і з яким коментарем."""
    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731
    return [
        dict(dev="SW-OBL-01", start=local(6, "10:05", now), minutes=45, metric="temperature",
             fn=lambda t, b: round(b + min(t, 25) * 0.9, 1),
             incidents=[dict(sev="warning", at=15, ack=("a.koval", 4), resolve=52, close=(58,
                         "Очищено пиловий фільтр і перевірено вентилятори шафи"))]),
        dict(dev="SW-TRO-01", start=local(6, "22:40", now), minutes=9, offline=True,
             incidents=[dict(sev=None, at=3, ack=("d.bondar", 4), resolve=9, close=(15,
                         "Обрив патч-корду на вузлі, корд замінено"))]),
        dict(dev="SW-VYN-01", start=local(5, "14:20", now), minutes=25, offline=True, reboot=True,
             incidents=[dict(sev=None, at=3, ack=("admin", 9), resolve=27, close=(35,
                         "Комутатор завис після оновлення ПЗ; перезавантажено, прошивку повернено"))]),
        dict(dev="RT-OBL-01", start=local(5, "03:15", now), minutes=30, metric="temperature",
             fn=lambda t, b: round(66.2 + 0.8 * (t % 3), 1),
             incidents=[dict(sev="warning", at=5, ack=("d.bondar", 3), resolve=33, close=(40,
                         "Нічне регламентне резервне копіювання; після нього температура в нормі"))]),
        dict(dev="UPS-POZ-01", start=local(4, "09:40", now), minutes=180, metric="battery_temperature",
             fn=lambda t, b: round(40.8 + 0.7 * min(t, 60) / 60, 1),
             incidents=[dict(sev="warning", at=5, ack=("m.shevchuk", 4), resolve=185, close=(200,
                         "Відремонтовано кондиціонер у шафі вузла"), telegram_fail=True)]),
        dict(dev="UPS-POZ-01", start=local(4, "18:10", now), minutes=20, metric="input_voltage",
             fn=lambda t, b: round(196.5 + (t % 4) * 0.4, 1),
             incidents=[dict(sev="warning", at=5, ack=("a.koval", 2), resolve=22, close=(30,
                         "Короткочасне просідання напруги в мережі живлення, ДБЖ відпрацював штатно"))]),
        # аварія живлення вузла Троєщина: мережа зникла на 130 хв, АКБ розрядилася до 32 %
        dict(dev="UPS-TRO-01", start=local(3, "02:10", now), minutes=130, metric="input_voltage",
             fn=lambda t, b: 0.0,
             incidents=[dict(sev="critical", at=1, ack=("d.bondar", 7), resolve=135, close=(150,
                             "Відновлено живлення вузла після аварії в міській мережі")),
                        dict(sev="warning", at=1, ack=("d.bondar", 7), resolve=135, close=(150,
                             "Відновлено живлення вузла після аварії в міській мережі"))]),
        dict(dev="UPS-TRO-01", start=local(3, "02:10", now), minutes=370, metric="battery_charge",
             fn=lambda t, b: round(100 - 68 * t / 130, 1) if t <= 130 else round(min(100, 32 + 68 * (t - 130) / 240), 1),
             incidents=[dict(sev="warning", at=100, ack=("d.bondar", 2), resolve=210, close=(240,
                             "АКБ заряджено після відновлення живлення, ємність у нормі"))]),
        dict(dev="UPS-TRO-01", start=local(3, "02:10", now), minutes=370, metric="battery_runtime",
             fn=lambda t, b: round(b * (1 - 0.68 * t / 130)) if t <= 130 else round(b * min(1, 0.32 + 0.68 * (t - 130) / 240)),
             incidents=[]),
        dict(dev="RT-DC-01", start=local(2, "11:00", now), minutes=18, metric="cpu_load",
             fn=lambda t, b: round(96.3 + (t % 3) * 0.6, 1),
             incidents=[dict(sev="critical", at=2, ack=("a.koval", 2), resolve=25, close=(40,
                             "Відфільтровано аномальний трафік (DDoS) на прикордонному маршрутизаторі")),
                        dict(sev="warning", at=2, ack=("a.koval", 3), resolve=25, close=(41,
                             "Відфільтровано аномальний трафік (DDoS) на прикордонному маршрутизаторі"))]),
        dict(dev="SW-VYN-03", start=local(2, "07:30", now), minutes=12, offline=True,
             incidents=[dict(sev=None, at=3, ack=("m.shevchuk", 3), resolve=13, close=(20,
                             "Планове переключення живлення шафи; оповіщення не вимкнули"))]),
        dict(dev="SW-DC-CORE-02", start=local(2, "19:20", now), minutes=12, metric="cpu_load",
             fn=lambda t, b: round(84.5 + (t % 4) * 0.5, 1),
             incidents=[dict(sev="warning", at=2, ack=("m.shevchuk", 3), resolve=14, close=(25,
                             "Перебудова STP після заміни аплінка; навантаження нормалізувалося"))]),
        dict(dev="UPS-VYN-01", start=local(1, "08:05", now), minutes=30, metric="battery_charge",
             fn=lambda t, b: round(max(47.2, 100 - 2.4 * t), 1),
             incidents=[dict(sev="warning", at=25, ack=("a.koval", 3), resolve=30, close=(45,
                             "Планове самотестування АКБ"))]),
        dict(dev="UPS-DC-01", start=local(1, "16:30", now), minutes=70, metric="output_load",
             fn=lambda t, b: round(84.2 + (t % 5) * 0.4, 1),
             incidents=[dict(sev="warning", at=5, ack=("m.shevchuk", 3), resolve=75)]),
        dict(dev="SW-POZ-02", start=ago(600), minutes=600, metric="temperature",
             fn=lambda t, b: round(61.8 + 0.7 * min(t, 30) / 30, 1),
             incidents=[dict(sev="warning", at=10, ack=("a.koval", 4))]),
        dict(dev="SW-OBL-02", start=ago(185), minutes=6, offline=True,
             incidents=[dict(sev=None, at=3, ack=("d.bondar", 2), resolve=8)]),
        dict(dev="UPS-OBL-01", start=ago(40), minutes=40, metric="battery_temperature",
             fn=lambda t, b: round(40.6 + 0.1 * (t % 3), 1),
             incidents=[dict(sev="warning", at=2)]),
    ]


def run_sql_file(conn, name: str):
    conn.execute((REPO_DIR / "sql" / name).read_text(encoding="utf-8"))


def apply_yaml(conn, path: str, user_id: int) -> str:
    cfg, sha = load((REPO_DIR / path).read_bytes())
    row = conn.execute("SELECT sp_apply_snmp_config(%s, %s, %s::jsonb, %s) AS r",
                       ("/etc/netguard/snmp_config.yaml", sha, to_json(cfg), user_id)).fetchone()
    return row["r"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="перестворити схему netguard (усі дані буде видалено)")
    ap.add_argument("--days", type=int, default=7, help="глибина історії показників, діб")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 5)
    conn = psycopg.connect(settings.dsn, row_factory=dict_row, autocommit=True)
    if args.reset:
        for f in ("schema.sql", "lr2_logic.sql", "seed_reference.sql"):
            print(f"→ sql/{f}")
            run_sql_file(conn, f)
    conn.execute("SET search_path TO netguard, public")
    conn.autocommit = False

    with conn.transaction():
        conn.execute("SELECT set_config('netguard.bypass_checks', 'on', true)")
        for loc in LOCATIONS:
            conn.execute("INSERT INTO locations (name, location_type, address, description) VALUES (%s, %s, %s, %s)"
                         " ON CONFLICT (name) DO NOTHING", loc)
        uid = {}
        for login, name, role, pwd, email, tg, active in USERS:
            row = conn.execute(
                "INSERT INTO users (role_id, full_name, login, password_hash, email, telegram_chat_id, is_active, created_at)"
                " VALUES ((SELECT id FROM roles WHERE code = %s), %s, %s, fn_hash_password(%s), %s, %s, %s, %s)"
                " ON CONFLICT (login) DO UPDATE SET full_name = EXCLUDED.full_name RETURNING id",
                (role, name, login, pwd, email, tg, active, now - timedelta(days=args.days + 2))).fetchone()
            uid[login] = row["id"]
        admin = uid["admin"]
        conn.execute("UPDATE event_log e SET created_at = u.created_at FROM users u"
                     " WHERE e.message = 'Створено користувача ' || u.login")
        conn.execute("SELECT set_config('netguard.user_id', %s, true)", (str(admin),))

        print("→ YAML-конфігурація:", apply_yaml(conn, "config/snmp_config.yaml", admin), "/",
              apply_yaml(conn, "config/snmp_config.demo.yaml", admin))
        conn.execute("UPDATE snmp_config SET loaded_at = %s WHERE id = (SELECT min(id) FROM snmp_config)",
                     (now - timedelta(days=args.days + 1, hours=3),))
        conn.execute("UPDATE snmp_config SET loaded_at = %s WHERE is_active", (now - timedelta(days=args.days, hours=20),))
        conn.execute("UPDATE event_log e SET created_at = c.loaded_at FROM snmp_config c"
                     " WHERE e.event_type = 'config_change' AND (e.details ->> 'snmp_config_id')::int = c.id")

        dev = {r["name"]: r for r in conn.execute(
            "SELECT e.id, e.name, t.category FROM equipment e JOIN equipment_type t ON t.id = e.type_id").fetchall()}
        conn.execute("SELECT set_config('netguard.sync', 'on', true)")    # без окремих записів аудиту
        for name, (model, serial, va, w, ah, cnt, volt, inst) in UPS.items():
            conn.execute("UPDATE equipment SET model = %s, serial_number = %s, installed_on = %s WHERE id = %s",
                         (model, serial, inst, dev[name]["id"]))
            conn.execute("INSERT INTO ups_details (equipment_id, rated_power_va, rated_power_w, battery_capacity_ah,"
                         " battery_count, nominal_voltage_v, battery_installed_on) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                         " ON CONFLICT (equipment_id) DO NOTHING", (dev[name]["id"], va, w, ah, cnt, volt, inst))
        conn.execute("SELECT set_config('netguard.sync', '', true)")

        thr = {}
        for prof, metric, sev, lo, hi in THRESHOLDS:
            row = conn.execute(
                "INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value, max_value)"
                " SELECT t.id, m.id, %s, %s, %s FROM equipment_type t, metric m WHERE t.snmp_profile = %s AND m.code = %s"
                " ON CONFLICT (equipment_type_id, metric_id, severity) DO UPDATE SET min_value = EXCLUDED.min_value"
                " RETURNING id", (sev, lo, hi, prof, metric)).fetchone()
            thr[(prof, metric, sev)] = row["id"]
        conn.execute("UPDATE event_log SET created_at = %s WHERE event_type = 'config_change' AND message LIKE '%%поріг%%'",
                     (now - timedelta(days=args.days, hours=19),))
        conn.execute("UPDATE threshold SET updated_at = %s", (now - timedelta(days=args.days, hours=19),))

        load_history(conn, dev, now, args.days)
        make_incidents(conn, dev, thr, uid, now)
        make_auth_events(conn, uid, now, args.days)
        conn.execute("UPDATE equipment SET status = 'online', failed_polls = 0, last_seen_at = %s, created_at = %s",
                     (now - timedelta(minutes=1), now - timedelta(days=args.days + 1)))

    counts = conn.execute(
        "SELECT (SELECT count(*) FROM equipment) eq, (SELECT count(*) FROM metric_reading) readings,"
        " (SELECT count(*) FROM incident) incidents, (SELECT count(*) FROM notification) notifications,"
        " (SELECT count(*) FROM event_log) events, (SELECT count(*) FROM users) users").fetchone()
    print("→ готово:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    conn.close()


def load_history(conn, dev: dict, now: datetime, days: int):
    """Історія показників із кроком 5 хв (COPY) з урахуванням аварійних сценаріїв."""
    plan = conn.execute("SELECT DISTINCT equipment_id, metric_id, metric_code, category, equipment_name"
                        " FROM v_snmp_poll_plan").fetchall()
    scen = scenarios(now)
    start = now - timedelta(days=days)
    reboots = {s["dev"]: s["start"] + timedelta(minutes=s["minutes"]) for s in scen if s.get("reboot")}
    offline = [(dev[s["dev"]]["id"], s["start"], s["start"] + timedelta(minutes=s["minutes"]))
               for s in scen if s.get("offline")]
    anomalies = {}
    for s in scen:
        if "metric" in s:
            anomalies.setdefault((dev[s["dev"]]["id"], s["metric"]), []).append(s)
    n = 0
    with conn.cursor().copy("COPY metric_reading (equipment_id, metric_id, read_at, value) FROM STDIN") as cp:
        for p in plan:
            rnd = random.Random(p["equipment_id"] * 1000 + p["metric_id"])
            boot_default = UPTIME_ANCHOR + timedelta(hours=7 * p["equipment_id"])
            ts = start
            while ts <= now:
                if any(e == p["equipment_id"] and a <= ts < b for e, a, b in offline):
                    ts += STEP
                    continue
                boot = boot_default
                rb = reboots.get(p["equipment_name"])
                if rb and ts >= rb:
                    boot = rb
                value = baseline(p["metric_code"], p["category"], p["equipment_id"], ts, rnd, boot)
                for s in anomalies.get((p["equipment_id"], p["metric_code"]), []):
                    t = (ts - s["start"]).total_seconds() / 60
                    if 0 <= t < s["minutes"]:
                        value = s["fn"](t, value)
                cp.write_row((p["equipment_id"], p["metric_id"], ts, value))
                n += 1
                ts += STEP
    print(f"→ показники: {n}")


def make_incidents(conn, dev: dict, thr: dict, uid: dict, now: datetime):
    support = [r for r in conn.execute(
        "SELECT u.id, u.email, u.telegram_chat_id FROM users u JOIN roles r ON r.id = u.role_id"
        " WHERE r.code = 'support' AND u.is_active").fetchall()]
    admins = [r for r in conn.execute(
        "SELECT u.id, u.email, u.telegram_chat_id FROM users u JOIN roles r ON r.id = u.role_id"
        " WHERE r.code = 'admin' AND u.is_active").fetchall()]
    prof = {r["name"]: r["snmp_profile"] for r in conn.execute(
        "SELECT e.name, t.snmp_profile FROM equipment e JOIN equipment_type t ON t.id = e.type_id").fetchall()}
    rnd = random.Random(42)
    count = 0
    for s in scenarios(now):
        d = dev[s["dev"]]
        for inc in s["incidents"]:
            opened = s["start"] + timedelta(minutes=inc["at"])
            if s.get("offline"):
                kind, th, value = "offline", None, None
            else:
                # момент відкриття — перший фактичний показник, що порушив поріг (як це зробив би тригер)
                kind = "threshold"
                th = thr[(prof[s["dev"]], s["metric"], inc["sev"])]
                first = conn.execute(
                    "SELECT r.read_at, r.value FROM metric_reading r JOIN metric m ON m.id = r.metric_id AND m.code = %s"
                    " JOIN threshold t ON t.id = %s WHERE r.equipment_id = %s AND r.read_at >= %s AND r.read_at < %s"
                    " AND (r.value < t.min_value OR r.value > t.max_value) ORDER BY r.read_at LIMIT 1",
                    (s["metric"], th, d["id"], opened, s["start"] + timedelta(minutes=s["minutes"]))).fetchone()
                if first is None:
                    raise RuntimeError(f"сценарій {s['dev']}/{s['metric']}: показники не порушують поріг {inc['sev']}")
                opened, value = first["read_at"] + timedelta(seconds=20), first["value"]
            ack = inc.get("ack")
            ack_at = opened + timedelta(minutes=ack[1]) if ack else None
            esc_at = None
            if (ack is None and now - opened > timedelta(minutes=5)) or (ack and ack[1] > 5):
                esc_at = opened + timedelta(minutes=5)
            res_at = opened + timedelta(minutes=inc["resolve"]) if inc.get("resolve") else None
            close = inc.get("close")
            closed_at = opened + timedelta(minutes=close[0]) if close else None
            status = ("closed" if close else "resolved" if res_at else "acknowledged" if ack
                      else "escalated" if esc_at else "new")
            row = conn.execute(
                "INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value, status, opened_at,"
                " escalated_at, acknowledged_at, acknowledged_by, resolved_at, closed_at, close_comment)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (d["id"], kind, th, value, status, opened, esc_at, ack_at, uid[ack[0]] if ack else None,
                 res_at, closed_at, close[1] if close else None)).fetchone()
            iid, count = row["id"], count + 1
            actor = uid[ack[0]] if ack else None

            def event(at, msg, user=None, details=None, etype="incident"):
                conn.execute("INSERT INTO event_log (created_at, event_type, user_id, equipment_id, incident_id, message,"
                             " details) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                             (at, etype, user, d["id"], iid, msg, Jsonb(details) if details else None))

            def notify(users, at, escalation=False):
                for u in users:
                    chans = ["web"] + (["email"] if u["email"] else []) + (["telegram"] if u["telegram_chat_id"] else [])
                    for ch in chans:
                        fail = ch == "telegram" and inc.get("telegram_fail") and u["id"] == uid["m.shevchuk"]
                        conn.execute(
                            "INSERT INTO notification (incident_id, user_id, channel, status, is_escalation, created_at,"
                            " sent_at, error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            (iid, u["id"], ch, "failed" if fail else "sent", escalation, at,
                             None if fail else at + timedelta(seconds=rnd.randint(1, 4)),
                             "Telegram API: Bad Request: chat not found" if fail else None))

            if kind == "offline":
                event(opened, f"{s['dev']}: online → offline", etype="status_change",
                      details={"from": "online", "to": "offline"})
                back = s["start"] + timedelta(minutes=s["minutes"])
                if back <= now:
                    event(back, f"{s['dev']}: offline → online", etype="status_change",
                          details={"from": "offline", "to": "online"})
            event(opened, f"Відкрито інцидент №{iid} ({kind}) на {s['dev']}",
                  details={"threshold_id": th, "value": value})
            notify(support, opened)
            if esc_at:
                event(esc_at, f"Інцидент №{iid}: new → escalated")
                notify(admins, esc_at, escalation=True)
            if ack_at:
                event(ack_at, f"Інцидент №{iid}: {'escalated' if esc_at else 'new'} → acknowledged", actor)
            if res_at:
                event(res_at, f"Інцидент №{iid}: acknowledged → resolved", actor)
            if closed_at:
                event(closed_at, f"Інцидент №{iid}: resolved → closed", actor, {"comment": close[1]})
    print(f"→ інциденти: {count}")


def make_auth_events(conn, uid: dict, now: datetime, days: int):
    rnd = random.Random(7)
    for day in range(days, -1, -1):
        for login in ("admin", "a.koval", "m.shevchuk", "d.bondar", "s.lysenko"):
            if login == "s.lysenko" and day % 2:
                continue
            at = local(day, f"{8 + rnd.randint(0, 1)}:{rnd.randint(0, 59):02d}", now)
            if at < now:
                conn.execute("INSERT INTO event_log (created_at, event_type, user_id, message) VALUES (%s, 'auth', %s,"
                             " 'Успішний вхід')", (at, uid[login]))
    conn.execute("INSERT INTO event_log (created_at, event_type, user_id, message, details) VALUES"
                 " (%s, 'auth', %s, 'Вхід заблокованого користувача відхилено', '{\"login\": \"i.tkachenko\"}')",
                 (local(2, "09:12", now), uid["i.tkachenko"]))
    conn.execute("INSERT INTO event_log (created_at, event_type, user_id, message, details) VALUES"
                 " (%s, 'user_action', %s, 'Змінено користувача i.tkachenko',"
                 " '{\"is_active\": {\"було\": true, \"стало\": false}}')", (local(3, "17:40", now), uid["admin"]))
    conn.execute("UPDATE users SET last_login_at = (SELECT max(created_at) FROM event_log e"
                 " WHERE e.user_id = users.id AND e.event_type = 'auth' AND e.message = 'Успішний вхід')")


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error as e:
        print(f"ПОМИЛКА БД: {e}", file=sys.stderr)
        sys.exit(1)

"""Polling Service — сервіс опитування обладнання (FR-04, SR-04.1, SR-04.2).

Цикл роботи:
  1. прочитати план опитування з представлення v_snmp_poll_plan (активні пристрої,
     облікові дані, OID і множники метрик — те, що задано в YAML-конфігурації);
  2. для кожного пристрою, якому настав час опитування (poll_interval_s), виконати SNMP GET
     усіх OID одним запитом;
  3. відповідь є: значення × scale записати в metric_reading (тригер БД перевіряє пороги
     й відкриває інциденти), викликати sp_register_poll_success;
     відповіді немає: викликати sp_register_poll_failure (після N невдач поспіль БД
     позначає пристрій offline і відкриває інцидент);
  4. план перечитується щохвилини, тож зміни YAML-конфігурації підхоплюються без перезапуску.

Якщо задано NETGUARD_SNMP_CONFIG (шлях до YAML-файлу), сервіс під час старту й щохвилини
порівнює SHA-256 файлу з востаннє обробленим і після зміни перевіряє файл та застосовує
його функцією БД sp_apply_snmp_config. Помилка у файлі чи в БД лише пишеться в лог —
чинною лишається попередня версія конфігурації.

Кожен пристрій опитується у власній транзакції: помилка одного не впливає на інші.
SNMP-агентів емулює netguard.services.emulator (у лабораторному стенді немає обладнання).

    python -m netguard.services.poller
"""
import hashlib
import logging
import signal
import time
from collections import defaultdict
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ..db import connect
from ..settings import settings
from ..snmp_yaml import ConfigError, load
from .emulator import Emulator, SnmpTimeout, parse_faults

log = logging.getLogger("netguard.poller")
PLAN_TTL = 60          # як часто перечитувати план опитування, с


class Poller:
    def __init__(self, driver: Emulator):
        self.driver = driver
        self.conn = connect(role="ng_poller", autocommit=True)
        self.plan: dict[int, list[dict]] = {}
        self.plan_loaded = 0.0
        self.next_due: dict[int, float] = {}
        self.config_sha: str | None = None    # SHA-256 востаннє обробленого вмісту YAML-файлу
        self.running = True

    def sync_config(self):
        path = settings.snmp_config
        if not path:
            return
        try:
            raw = path.read_bytes()
        except OSError as e:
            log.error("YAML-конфігурацію не прочитано: %s", e)
            return
        sha = hashlib.sha256(raw).hexdigest()
        if sha == self.config_sha:
            return
        self.config_sha = sha                 # помилковий файл не перевіряємо щохвилини знову
        try:
            cfg, sha = load(raw)
            with self.conn.transaction():
                res = self.conn.execute("SELECT sp_apply_snmp_config(%s, %s, %s, NULL) AS r",
                                        (str(path), sha, Jsonb(cfg))).fetchone()["r"]
        except ConfigError as e:
            log.error("YAML-конфігурацію не застосовано, помилка у файлі: %s", e)
            return
        except Exception as e:              # порушення обмежень БД: транзакцію відкочено
            log.error("YAML-конфігурацію не застосовано, помилка БД: %s", str(e).strip())
            return
        log.info("YAML-конфігурація %s (SHA-256 %s…): %s", path.name, sha[:12],
                 {"skipped": "уже активна", "reactivated": "активовано раніше застосовану версію",
                  "applied": "застосовано нову версію"}[res])

    def load_plan(self):
        rows = self.conn.execute("SELECT * FROM v_snmp_poll_plan ORDER BY equipment_id, metric_id").fetchall()
        plan = defaultdict(list)
        for r in rows:
            plan[r["equipment_id"]].append(r)
        self.plan = dict(plan)
        self.plan_loaded = time.monotonic()
        log.info("план опитування: %d пристроїв, %d метрик", len(self.plan), len(rows))

    def poll_device(self, oids: list[dict]):
        dev = oids[0]
        ts = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            raw = self.driver.get(dev, oids, ts)
        except SnmpTimeout as e:
            with self.conn.transaction():
                self.conn.execute("CALL sp_register_poll_failure(%s)", (dev["equipment_id"],))
            log.warning("%s: немає відповіді (%s)", dev["equipment_name"], e)
            return
        rows = [(dev["equipment_id"], o["metric_id"], ts, round(float(raw[o["oid"]]) * float(o["scale"]), 3))
                for o in oids if o["oid"] in raw]
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.executemany("INSERT INTO metric_reading (equipment_id, metric_id, read_at, value)"
                                " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", rows)
            self.conn.execute("CALL sp_register_poll_success(%s)", (dev["equipment_id"],))
        log.debug("%s: %d показників", dev["equipment_name"], len(rows))

    def run(self):
        while self.running:
            if time.monotonic() - self.plan_loaded > PLAN_TTL:
                self.sync_config()
                self.load_plan()
            now = time.monotonic()
            for eq_id, oids in self.plan.items():
                if self.next_due.get(eq_id, 0) <= now:
                    self.next_due[eq_id] = now + oids[0]["poll_interval_s"]
                    try:
                        self.poll_device(oids)
                    except Exception:          # помилка БД для одного пристрою не зупиняє сервіс
                        log.exception("помилка опитування %s", oids[0]["equipment_name"])
            time.sleep(1)


def boot_times(conn) -> dict:
    """Момент старту кожного агента — з останнього значення uptime (неперервність історії)."""
    rows = conn.execute(
        "SELECT DISTINCT ON (r.equipment_id) r.equipment_id, r.read_at - make_interval(secs => r.value) AS boot"
        " FROM metric_reading r JOIN metric m ON m.id = r.metric_id AND m.code = 'uptime'"
        " WHERE r.read_at > now() - INTERVAL '2 days' ORDER BY r.equipment_id, r.read_at DESC").fetchall()
    return {r["equipment_id"]: r["boot"] for r in rows}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    probe = connect(role="ng_poller", autocommit=True)
    driver = Emulator(parse_faults(settings.sim_faults), boot_times(probe))
    probe.close()
    poller = Poller(driver)
    signal.signal(signal.SIGTERM, lambda *_: setattr(poller, "running", False))
    log.info("Polling Service запущено (емулятор SNMP, несправності: %s)", driver.faults or "немає")
    try:
        poller.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Alerting Service — сервіс аналізу аварій.

Порівняння показників з порогами виконує сама БД: тригер reading_check_thresholds на
metric_reading відкриває інцидент у тій самій транзакції, де записано показник
(без затримки й без дублювання — uq_incident_open). Сервіс відповідає за те, що
потребує часу:
  * ескалація — кожні 15 с викликає fn_escalate_overdue: інциденти в статусі new,
    не прийняті за NETGUARD_ESCALATE_AFTER_MIN хвилин (типово 5), переводяться в
    escalated, і БД ставить у чергу сповіщення адміністраторам;
  * журнал роботи — слухає канал ng_incident і пише у свій лог кожен новий інцидент
    та зміну його статусу.

    python -m netguard.services.alerting
"""
import logging
import signal
import time

from ..db import connect
from ..settings import settings

log = logging.getLogger("netguard.alerting")
TICK = 15


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    conn = connect(role="ng_alerting", autocommit=True)
    conn.execute("LISTEN ng_incident")
    state = {"running": True}
    signal.signal(signal.SIGTERM, lambda *_: state.update(running=False))
    log.info("Alerting Service запущено (ескалація через %d хв)", settings.escalate_after_min)
    next_tick = 0.0
    try:
        while state["running"]:
            if time.monotonic() >= next_tick:
                n = conn.execute("SELECT fn_escalate_overdue(make_interval(mins => %s)) AS n",
                                 (settings.escalate_after_min,)).fetchone()["n"]
                if n:
                    log.warning("ескальовано інцидентів: %d", n)
                next_tick = time.monotonic() + TICK
            # notifies() утримує з'єднання, тож сигнали спершу збираємо, а потім обробляємо
            for note in list(conn.notifies(timeout=5, stop_after=1)):
                row = conn.execute("SELECT id, status, incident_type, equipment, severity, metric_code, metric_value"
                                   " FROM v_incident_list WHERE id = %s", (int(note.payload),)).fetchone()
                if row:
                    log.info("інцидент №%s %s: %s %s %s %s", row["id"], row["status"], row["equipment"],
                             row["incident_type"], row["metric_code"] or "", row["metric_value"] or "")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

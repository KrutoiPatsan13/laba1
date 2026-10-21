"""Notification Service — сервіс сповіщень (FR-07, SR-07.1, SR-07.2).

Черга сповіщень — таблиця notification (status = 'queued'). Рядки в неї ставить БД
(функція fn_enqueue_notifications з антифлуд-перевіркою), а потім подає сигнал
pg_notify('ng_notification'). Сервіс слухає цей канал (LISTEN), забирає сповіщення
з черги (FOR UPDATE SKIP LOCKED — кілька екземплярів сервісу не надішлють одне
сповіщення двічі), надсилає його відповідним каналом і фіксує результат:
sent + sent_at або failed + error_message.

Канали: web — сповіщення показується у веб-інтерфейсі; email — SMTP; telegram —
Telegram Bot API. Якщо NETGUARD_NOTIFY_DRY_RUN=1, e-mail і Telegram не надсилаються,
а дописуються у var/outbox.log (демонстраційний стенд без зовнішніх облікових записів).

    python -m netguard.services.notifier
"""
import json
import logging
import signal
import smtplib
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from ..db import connect
from ..settings import settings

log = logging.getLogger("netguard.notifier")

QUEUE_SQL = """
SELECT n.id, n.channel, n.is_escalation, u.full_name, u.email, u.telegram_chat_id,
       i.id AS incident_id, i.incident_type, i.metric_value, e.name AS equipment, l.name AS location,
       t.severity, m.name AS metric_name, m.unit, t.min_value, t.max_value
  FROM notification n
  JOIN users u ON u.id = n.user_id
  JOIN incident i ON i.id = n.incident_id
  JOIN equipment e ON e.id = i.equipment_id
  JOIN locations l ON l.id = e.location_id
  LEFT JOIN threshold t ON t.id = i.threshold_id
  LEFT JOIN metric m ON m.id = t.metric_id
 WHERE n.status = 'queued'
 ORDER BY n.id
 LIMIT 20
   FOR UPDATE OF n SKIP LOCKED
"""


class DeliveryError(Exception):
    pass


def message_text(n: dict) -> str:
    head = "ЕСКАЛАЦІЯ: " if n["is_escalation"] else ""
    if n["incident_type"] == "offline":
        what = "пристрій не відповідає на SNMP-запити"
    else:
        bounds = " … ".join(str(x) for x in (n["min_value"], n["max_value"]) if x is not None)
        what = f"{n['metric_name']} = {n['metric_value']:g} {n['unit'] or ''} (норма: {bounds})"
    sev = {"critical": "КРИТИЧНО", "warning": "Попередження"}.get(n["severity"], "Аварія")
    return f"{head}[{sev}] {n['equipment']} ({n['location']}): {what}. Інцидент №{n['incident_id']}."


class Notifier:
    def __init__(self):
        self.conn = connect(role="ng_notifier", autocommit=True)
        self.conn.execute("LISTEN ng_notification")
        self.running = True

    # ------------------------------------------------------------ канали доставки
    def outbox(self, channel: str, to: str, text: str):
        settings.var_dir.mkdir(parents=True, exist_ok=True)
        with open(settings.var_dir / "outbox.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {channel} → {to}: {text}\n")

    def send_email(self, n: dict, text: str):
        if settings.smtp_host:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = f"NetGuard: інцидент №{n['incident_id']}", settings.smtp_from, n["email"]
            msg.set_content(text)
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        elif settings.notify_dry_run:
            self.outbox("email", n["email"], text)
        else:
            raise DeliveryError("SMTP не налаштовано (SMTP_HOST)")

    def send_telegram(self, n: dict, text: str):
        if settings.telegram_token:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                data=json.dumps({"chat_id": n["telegram_chat_id"], "text": text}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if not json.load(r).get("ok"):
                    raise DeliveryError("Telegram API відхилив повідомлення")
        elif settings.notify_dry_run:
            self.outbox("telegram", str(n["telegram_chat_id"]), text)
        else:
            raise DeliveryError("Telegram не налаштовано (TELEGRAM_BOT_TOKEN)")

    def deliver(self, n: dict):
        text = message_text(n)
        if n["channel"] == "email":
            self.send_email(n, text)
        elif n["channel"] == "telegram":
            self.send_telegram(n, text)
        # web: сповіщення вже в БД, веб-інтерфейс показує його користувачу

    # ------------------------------------------------------------ обробка черги
    def process_queue(self) -> int:
        total = 0
        while True:
            with self.conn.transaction():
                batch = self.conn.execute(QUEUE_SQL).fetchall()
                for n in batch:
                    try:
                        self.deliver(n)
                        self.conn.execute("UPDATE notification SET status = 'sent', sent_at = now() WHERE id = %s",
                                          (n["id"],))
                    except Exception as e:      # невдала доставка не зупиняє обробку черги
                        self.conn.execute("UPDATE notification SET status = 'failed', error_message = %s WHERE id = %s",
                                          (str(e)[:500] or type(e).__name__, n["id"]))
                        log.warning("сповіщення %s (%s) не доставлено: %s", n["id"], n["channel"], e)
            total += len(batch)
            if not batch:
                return total

    def run(self):
        while self.running:
            sent = self.process_queue()
            if sent:
                log.info("оброблено сповіщень: %d", sent)
            for _ in self.conn.notifies(timeout=5, stop_after=1):   # чекаємо сигналу від БД
                pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    notifier = Notifier()
    signal.signal(signal.SIGTERM, lambda *_: setattr(notifier, "running", False))
    log.info("Notification Service запущено (dry-run: %s)", settings.notify_dry_run)
    try:
        notifier.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

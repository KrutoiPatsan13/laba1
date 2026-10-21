"""Запуск усіх мікросервісів NetGuard Monitor окремими процесами (аналог docker compose up).

    python -m netguard                  # усі сервіси
    python -m netguard poller notifier  # лише вказані сервіси

Сервіси: poller (Polling Service), alerting (Alerting Service), notifier (Notification Service),
reporting (Reporting Service, :8082), gateway (API Gateway / Auth Service, :8081),
web (Web UI, :8080). Кожен — окремий процес зі своєю роллю СУБД; Web UI до БД не підключається.
Ctrl+C зупиняє всі процеси. Якщо секрети NETGUARD_JWT_SECRET і NETGUARD_STORAGE_SECRET не задано,
запускач генерує випадкові на час роботи (після перезапуску користувачі входять знову).
"""
import os
import secrets
import signal
import subprocess
import sys
import time

from .db import open_connection

SERVICES = {
    "poller": "netguard.services.poller",
    "alerting": "netguard.services.alerting",
    "notifier": "netguard.services.notifier",
    "reporting": "netguard.reporting",
    "gateway": "netguard.gateway",
    "web": "netguard.web",
}


def main():
    names = sys.argv[1:] or list(SERVICES)
    unknown = set(names) - set(SERVICES)
    if unknown:
        sys.exit(f"невідомі сервіси: {', '.join(sorted(unknown))}; доступні: {', '.join(SERVICES)}")
    if set(names) - {"web"}:
        open_connection().close()       # БД недоступна — одне зрозуміле повідомлення замість помилок кожного сервісу
    for var in ("NETGUARD_JWT_SECRET", "NETGUARD_STORAGE_SECRET"):
        if not os.environ.get(var):
            os.environ[var] = secrets.token_urlsafe(32)     # спільний для дочірніх процесів цього запуску
            print(f"{var} не задано — згенеровано випадковий на час роботи", flush=True)
    procs = {n: subprocess.Popen([sys.executable, "-m", SERVICES[n]]) for n in names}
    print(f"запущено: {', '.join(f'{n} (pid {p.pid})' for n, p in procs.items())}", flush=True)

    def stop(*_, code: int = 0):
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(code)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        for n, p in procs.items():
            if p.poll() is not None:
                print(f"сервіс {n} завершився з кодом {p.returncode}; зупиняю решту", flush=True)
                stop(code=p.returncode or 1)        # ненульовий код — щоб supervisor перезапустив систему
        time.sleep(1)


if __name__ == "__main__":
    main()

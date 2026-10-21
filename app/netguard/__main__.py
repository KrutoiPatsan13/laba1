"""Запуск усіх сервісів NetGuard Monitor окремими процесами (аналог docker compose up).

    python -m netguard                  # Polling, Alerting, Notification Service і веб-інтерфейс
    python -m netguard poller notifier  # лише вказані сервіси

Ctrl+C зупиняє всі процеси.
"""
import signal
import subprocess
import sys
import time

SERVICES = {
    "poller": "netguard.services.poller",
    "alerting": "netguard.services.alerting",
    "notifier": "netguard.services.notifier",
    "web": "netguard.web",
}


def main():
    names = sys.argv[1:] or list(SERVICES)
    unknown = set(names) - set(SERVICES)
    if unknown:
        sys.exit(f"невідомі сервіси: {', '.join(sorted(unknown))}; доступні: {', '.join(SERVICES)}")
    procs = {n: subprocess.Popen([sys.executable, "-m", SERVICES[n]]) for n in names}
    print(f"запущено: {', '.join(f'{n} (pid {p.pid})' for n, p in procs.items())}", flush=True)

    def stop(*_):
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        for n, p in procs.items():
            if p.poll() is not None:
                print(f"сервіс {n} завершився з кодом {p.returncode}; зупиняю решту", flush=True)
                stop()
        time.sleep(1)


if __name__ == "__main__":
    main()

# NetGuard Monitor — образ мікросервісів. Образ один для всіх сервісів (Polling, Alerting,
# Notification, Reporting, API Gateway, Web UI); який сервіс запустити, задає команда
# в docker-compose.yml. Без команди запускаються всі сервіси в одному контейнері.
#
#     docker build -t netguard-monitor:1.0 .
FROM python:3.13-slim

# fonts-dejavu-core — шрифти з кирилицею для звітів PDF (Reporting Service);
# postgresql-client — pg_dump і pg_restore для python -m netguard.maintenance. Версія клієнта
# має бути не нижчою за версію сервера PostgreSQL 16 (Debian 13 у базовому образі дає 17),
# інакше pg_dump відмовляється працювати — тому збірка перевіряє версію
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core postgresql-client tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && { pg_dump --version | grep -Eq 'PostgreSQL\) (1[6-9]|[2-9][0-9])\.' \
      || { echo "потрібен pg_dump версії 16 або новішої" >&2; exit 1; }; }

# сервіси працюють від імені непривілейованого користувача; /var/lib/netguard — його дані
# (outbox.log сервісу сповіщень, сесії веб-інтерфейсу, резервні копії БД)
RUN useradd --system --uid 10001 --user-group --home-dir /var/lib/netguard --no-create-home \
        --shell /usr/sbin/nologin netguard \
 && install -d -o netguard -g netguard /var/lib/netguard /var/lib/netguard/backup

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/netguard/app \
    TZ=Europe/Kyiv \
    NETGUARD_VAR_DIR=/var/lib/netguard \
    NICEGUI_STORAGE_PATH=/var/lib/netguard/nicegui

WORKDIR /opt/netguard
COPY app/requirements.txt app/
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r app/requirements.txt
COPY app/netguard app/netguard/

USER netguard
EXPOSE 8080 8081 8082
CMD ["python", "-m", "netguard"]

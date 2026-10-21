#!/usr/bin/env bash
# Початкове розгортання БД NetGuard Monitor у контейнері postgres:16.
#
# Образ postgres виконує скрипти з /docker-entrypoint-initdb.d лише під час першого старту
# (том даних порожній) від імені POSTGRES_USER — він стає власником схеми netguard, а сервіси
# перемикаються на свої ролі ng_* самі. Каталог sql/ репозиторію змонтовано лише для читання
# в /opt/netguard/sql. Якщо скрипт завершився помилкою, контейнер зупиняється, а повторно
# розгортання виконається лише на порожньому томі: усуньте причину й видаліть том
# (docker compose down -v).
#
# Якщо задано NETGUARD_ADMIN_PASSWORD, створюється перший адміністратор (логін
# NETGUARD_ADMIN_LOGIN, типово admin) — решту користувачів він додає у веб-інтерфейсі.

# тіло функції в дужках виконується в підоболонці: set не впливає на docker-entrypoint.sh,
# коли скрипт без права виконання і образ підключає його командою «.»
netguard_initdb() (
    set -Eeuo pipefail
    psql=(psql -v ON_ERROR_STOP=1 --no-psqlrc --no-password --quiet
          --username "$POSTGRES_USER" --dbname "$POSTGRES_DB")

    for f in schema.sql lr2_logic.sql seed_reference.sql; do
        echo "netguard-initdb: sql/$f"
        "${psql[@]}" -f "/opt/netguard/sql/$f"
    done

    if [[ -n ${NETGUARD_ADMIN_PASSWORD:-} ]]; then
        echo "netguard-initdb: перший адміністратор ${NETGUARD_ADMIN_LOGIN:-admin}"
        "${psql[@]}" -v login="${NETGUARD_ADMIN_LOGIN:-admin}" -v password="$NETGUARD_ADMIN_PASSWORD" <<'SQL'
INSERT INTO netguard.users (role_id, full_name, login, password_hash)
SELECT id, 'Адміністратор NetGuard', :'login', netguard.fn_hash_password(:'password')
  FROM netguard.roles WHERE code = 'admin';
SQL
    fi
)

netguard_initdb

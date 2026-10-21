#!/usr/bin/env bash
# Початкове розгортання БД NetGuard Monitor у контейнері postgres:16.
#
# Образ postgres виконує скрипти з /docker-entrypoint-initdb.d лише під час першого старту
# (том даних порожній) від імені POSTGRES_USER — він стає власником схеми netguard, а сервіси
# перемикаються на свої ролі ng_* самі. Каталог sql/ репозиторію змонтовано лише для читання
# в /opt/netguard/sql. Якщо скрипт завершився помилкою, контейнер зупиняється, а повторно
# розгортання виконається лише на порожньому томі: усуньте причину й видаліть том даних БД
# (docker compose down && docker volume rm netguard_pgdata; «down -v» видалив би й резервні копії).

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
)

netguard_initdb

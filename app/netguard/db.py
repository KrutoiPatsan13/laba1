"""Доступ до PostgreSQL для сервісів і веб-інтерфейсу.

Кожен сервіс працює під власною роллю СУБД (ng_web, ng_poller, ng_alerting, ng_notifier, ng_report —
див. sql/lr2_logic.sql), тож права обмежені за принципом найменших привілеїв.
Ідентифікатор користувача, від імені якого виконується транзакція, передається в БД
змінною сеансу netguard.user_id — її читають тригери аудиту журналу подій.
"""
import logging
import time
from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from .settings import settings

log = logging.getLogger("netguard.db")


ROLES = {"ng_web", "ng_poller", "ng_alerting", "ng_notifier", "ng_report"}


def _role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"невідома роль СУБД {role!r}")
    return role


def _session_sql(role: str | None) -> str:
    sql = "SET search_path TO netguard, public; SET TIME ZONE 'Europe/Kyiv';"
    if role:
        sql += f" SET ROLE {_role(role)};"
    return sql


# ---------------------------------------------------------------- очікування БД під час старту
def db_address() -> str:
    """Адреса БД з NETGUARD_DSN для повідомлень — без пароля."""
    try:
        p = conninfo_to_dict(settings.dsn)
    except psycopg.ProgrammingError:
        return "NETGUARD_DSN"
    return f"{p.get('host') or 'локальний сокет'}:{p.get('port') or 5432}, БД {p.get('dbname') or '—'}"


def unavailable(reason: str) -> SystemExit:
    return SystemExit(
        f"PostgreSQL недоступна ({db_address()}): {reason}\n"
        "Запустіть PostgreSQL 15+ і задайте NETGUARD_DSN, напр. у Docker:\n"
        "  docker run -d --name netguard-db -e POSTGRES_USER=netguard -e POSTGRES_PASSWORD=netguard \\\n"
        "    -e POSTGRES_DB=netguard -p 127.0.0.1:5432:5432 postgres:16\n"
        '  export NETGUARD_DSN="host=localhost dbname=netguard user=netguard password=netguard"\n'
        "(докладніше — README, розділ «Запуск»)")


def open_connection() -> psycopg.Connection:
    """З'єднання з БД; поки БД стартує (контейнер, перезапуск сервера), чекає до NETGUARD_DB_WAIT_S секунд."""
    deadline = time.monotonic() + settings.db_wait_s
    while True:
        try:
            return psycopg.connect(settings.dsn, row_factory=dict_row, autocommit=True)
        except psycopg.OperationalError as e:
            if time.monotonic() >= deadline:
                raise unavailable(str(e).strip().splitlines()[0]) from None
            log.warning("PostgreSQL недоступна (%s), повтор через 2 с", db_address())
            time.sleep(2)


# ---------------------------------------------------------------- синхронний доступ (фонові сервіси)
def connect(role: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    conn = open_connection()
    conn.execute(_session_sql(role))
    conn.autocommit = autocommit
    return conn


@contextmanager
def transaction(conn: psycopg.Connection, user_id: int | None = None):
    """Транзакція на наявному з'єднанні; помилка — відкат."""
    with conn.transaction():
        if user_id is not None:
            conn.execute("SELECT set_config('netguard.user_id', %s, true)", (str(user_id),))
        yield conn


# ---------------------------------------------------------------- асинхронний доступ (веб-інтерфейс)
class Database:
    def __init__(self, role: str = "ng_web"):
        self.role = role
        self.pool: AsyncConnectionPool | None = None

    async def open(self):
        async def configure(conn):
            await conn.set_autocommit(True)
            await conn.execute(_session_sql(self.role))
            await conn.set_autocommit(False)

        self.pool = AsyncConnectionPool(settings.dsn, min_size=1, max_size=8, open=False,
                                        kwargs={"row_factory": dict_row}, configure=configure)
        await self.pool.open()
        try:
            await self.pool.wait(timeout=settings.db_wait_s)
        except PoolTimeout:
            await self.pool.close()
            err = unavailable(f"немає з'єднання за {settings.db_wait_s:.0f} с")
            log.error("%s", err.code)
            raise err from None

    async def close(self):
        if self.pool:
            await self.pool.close()

    @asynccontextmanager
    async def tx(self, user_id: int | None = None, read_only: bool = False, role: str | None = None):
        """Одна транзакція: від імені користувача user_id; read_only — лише читання."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")
                if role:
                    await conn.execute(f"SET LOCAL ROLE {_role(role)}")
                if user_id is not None:
                    await conn.execute("SELECT set_config('netguard.user_id', %s, true)", (str(user_id),))
                yield conn

    async def all(self, sql, params=None, user_id=None) -> list[dict]:
        async with self.tx(user_id=user_id, read_only=user_id is None) as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def one(self, sql, params=None, user_id=None) -> dict | None:
        async with self.tx(user_id=user_id, read_only=user_id is None) as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def run(self, sql, params=None, user_id=None) -> dict | None:
        """Зміна даних від імені користувача; повертає перший рядок RETURNING (якщо є)."""
        async with self.tx(user_id=user_id) as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone() if cur.description else None


def error_text(exc: Exception) -> str:
    """Зрозуміле повідомлення про помилку БД для інтерфейсу."""
    if isinstance(exc, psycopg.Error) and exc.diag.message_primary:
        msg = exc.diag.message_primary
        if exc.diag.constraint_name:
            msg += f" (обмеження {exc.diag.constraint_name})"
        return msg
    return str(exc)

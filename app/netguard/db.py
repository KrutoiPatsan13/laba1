"""Доступ до PostgreSQL для сервісів і веб-інтерфейсу.

Кожен сервіс працює під власною роллю СУБД (ng_web, ng_poller, ng_alerting, ng_notifier, ng_report —
див. sql/lr2_logic.sql), тож права обмежені за принципом найменших привілеїв.
Ідентифікатор користувача, від імені якого виконується транзакція, передається в БД
змінною сеансу netguard.user_id — її читають тригери аудиту журналу подій.
"""
from contextlib import asynccontextmanager, contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .settings import settings


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


# ---------------------------------------------------------------- синхронний доступ (фонові сервіси)
def connect(role: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(settings.dsn, row_factory=dict_row, autocommit=True)
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

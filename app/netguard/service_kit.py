"""Спільне для HTTP-мікросервісів (API Gateway, Reporting Service): автентифікація за
токеном, перетворення помилок БД на відповіді HTTP, запуск uvicorn.

Помилка БД повертається як JSON {"detail": текст, "sqlstate": код, "constraint": обмеження}:
порушення обмежень і бізнес-правил тригерів — 409, некоректні дані — 422, нестача прав
ролі СУБД — 403. Так веб-інтерфейс показує користувачу ту саму причину, що й СУБД.
"""
import logging

import jwt
import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import Principal, verify
from .db import error_text

log = logging.getLogger("netguard.http")


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Потрібен токен доступу", headers={"WWW-Authenticate": "Bearer"})
    return token


def token_principal(request: Request) -> Principal:
    """Користувач із токена (без звернення до БД) — для сервісів, що лише читають дані."""
    try:
        return verify(bearer_token(request))
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Токен недійсний або прострочений", headers={"WWW-Authenticate": "Bearer"}) from None


def db_error_status(exc: psycopg.Error) -> int:
    state = exc.sqlstate or ""
    if not state and isinstance(exc, psycopg.DataError):
        return 422                             # відхилено ще драйвером, напр. символ NUL у тексті
    if state == "42501":                       # insufficient_privilege
        return 403
    if state.startswith("23") or state == "P0001" or state == "40001":
        return 409                             # обмеження цілісності, RAISE EXCEPTION тригерів, конфлікт транзакцій
    if state.startswith("22"):
        return 422                             # некоректні дані (формат, діапазон)
    return 500


def install_error_handlers(app: FastAPI):
    @app.exception_handler(psycopg.Error)
    async def _db_error(request: Request, exc: psycopg.Error):
        status = db_error_status(exc)
        if status == 500:
            log.exception("помилка БД у %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=status, content={
            "detail": error_text(exc), "sqlstate": exc.sqlstate,
            "constraint": exc.diag.constraint_name if exc.diag else None})


def run(app: FastAPI, port: int, host: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)     # без запису про кожен запит між сервісами
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)

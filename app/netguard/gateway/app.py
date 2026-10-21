"""API Gateway / Auth Service — єдина точка входу до системи для веб-інтерфейсу.

Автентифікує користувачів (пароль перевіряє функція БД fn_authenticate, сервіс видає
токен JWT), перевіряє ролі «Сапорт» і «Адміністратор», виконує операції з довідниками,
обладнанням та інцидентами від імені користувача (роль СУБД ng_web; автор змін іде в журнал
аудиту) і маршрутизує запити звітів до Reporting Service.

    python -m netguard.gateway            # http://127.0.0.1:8081, документація API — /docs
"""
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from ..service_kit import install_error_handlers
from ..settings import settings
from . import deps
from .routers import auth, equipment, events, incidents, notifications, queries, reports, snmp, thresholds, users


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await deps.db.open()
    deps.reporting = httpx.AsyncClient(base_url=settings.reporting_url, timeout=60)
    yield
    await deps.reporting.aclose()
    await deps.db.close()


app = FastAPI(title="NetGuard API Gateway", version="1.0", lifespan=lifespan)
install_error_handlers(app)
for module in (auth, equipment, incidents, notifications, events, thresholds, users, snmp, queries, reports):
    app.include_router(module.router)


@app.get("/health")
async def health():
    await deps.db.one("SELECT 1 AS ok")
    return {"status": "ok", "service": "gateway"}

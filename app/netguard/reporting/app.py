"""Reporting Service — мікросервіс звітності (FR-10, SR-10.1).

Формує звіти за період (доступність обладнання, інциденти з MTTA і MTTR, стан АКБ ДБЖ) і
файли Excel, PDF та CSV. Працює під роллю СУБД ng_report (лише читання), тож навіть помилка
в цьому сервісі не може змінити дані. Запити приймає від API Gateway з токеном користувача.

    python -m netguard.reporting          # http://127.0.0.1:8082
"""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response

from ..auth import Principal
from ..db import Database
from ..service_kit import install_error_handlers, token_principal
from . import build as rp

db = Database("ng_report")

MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf", "csv": "text/csv; charset=utf-8"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.open()
    yield
    await db.close()


app = FastAPI(title="NetGuard Reporting Service", version="1.0", lifespan=lifespan)
install_error_handlers(app)


def period(p_from: datetime, p_to: datetime) -> tuple[datetime, datetime]:
    if p_from.tzinfo is None or p_to.tzinfo is None:
        raise HTTPException(422, "Межі періоду потрібно задати з часовим поясом (ISO 8601)")
    if p_from >= p_to:
        raise HTTPException(422, "Початок періоду має бути раніше за кінець")
    return p_from, p_to


@app.get("/health")
async def health():
    await db.one("SELECT 1 AS ok")
    return {"status": "ok", "service": "reporting"}


@app.get("/reports")
async def reports(p_from: datetime = Query(alias="from"), p_to: datetime = Query(alias="to"),
                  _user: Principal = Depends(token_principal)):
    f, t = period(p_from, p_to)
    return {"period": rp.period_title(f, t), "reports": [asdict(r) for r in await rp.build(db, f, t)]}


@app.get("/reports/export")
async def export(fmt: Literal["xlsx", "pdf", "csv"], p_from: datetime = Query(alias="from"),
                 p_to: datetime = Query(alias="to"), key: str | None = None,
                 _user: Principal = Depends(token_principal)):
    f, t = period(p_from, p_to)
    reports = await rp.build(db, f, t)
    if fmt == "csv":
        rep = next((r for r in reports if r.key == key), None)
        if rep is None:
            raise HTTPException(404, f"Невідомий звіт {key!r}")
        body, name = rp.to_csv(rep), f"netguard_{rep.key}.csv"
    elif fmt == "xlsx":
        body, name = rp.to_xlsx(reports, rp.period_title(f, t)), "netguard_report.xlsx"
    else:
        body, name = rp.to_pdf(reports, rp.period_title(f, t)), "netguard_report.pdf"
    return Response(body, media_type=MEDIA[fmt], headers={"Content-Disposition": f'attachment; filename="{name}"'})

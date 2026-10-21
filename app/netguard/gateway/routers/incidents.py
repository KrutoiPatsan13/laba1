"""Інциденти та їхній життєвий цикл (FR-07, FR-08): список, картка, дії інженера.

Прийняти в роботу, позначити усунутим і закрити з коментарем можуть обидві ролі; дії виконують
процедури БД від імені користувача з токена, тож автор потрапляє в інцидент і в журнал подій.
"""
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ...auth import Principal
from ..deps import current_user, db

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

OPEN = ["new", "escalated", "acknowledged"]
STATUSES = OPEN + ["resolved", "closed"]

IncidentId = Annotated[int, Path(ge=1, le=2**63 - 1)]      # діапазон BIGINT


class CloseIn(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


@router.get("")
async def incident_list(status: Literal["open", "all", "new", "escalated", "acknowledged", "resolved", "closed"] = "open",
                        location_id: int | None = None, days: int = Query(7, ge=1, le=3650),
                        _user: Principal = Depends(current_user)):
    """Інциденти, відкриті за останні days діб; open — нові, ескальовані й прийняті в роботу, all — усі."""
    statuses = OPEN if status == "open" else STATUSES if status == "all" else [status]
    return await db.all(
        "SELECT * FROM v_incident_list WHERE status::text = ANY(%s) AND (%s::int IS NULL OR location_id = %s)"
        " AND opened_at > now() - make_interval(days => %s) ORDER BY opened_at DESC",
        (statuses, location_id, location_id, days))


@router.get("/{inc_id}")
async def incident_card(inc_id: IncidentId, _user: Principal = Depends(current_user)):
    i = await db.one("SELECT * FROM v_incident_list WHERE id = %s", (inc_id,))
    if not i:
        raise HTTPException(404, "Інцидент не знайдено")
    return i


@router.get("/{inc_id}/history")
async def history(inc_id: IncidentId, _user: Principal = Depends(current_user)):
    """Показник порушеного порогу: від 3 год до аварії до 1 год після усунення, крок 5 хв (для offline — порожньо)."""
    return await db.all(
        "SELECT h.* FROM v_incident_list i,"
        " fn_metric_history(i.equipment_id, i.metric_code, i.opened_at - INTERVAL '3 hours',"
        "                   coalesce(i.closed_at, i.resolved_at, now()) + INTERVAL '1 hour', INTERVAL '5 minutes') h"
        " WHERE i.id = %s AND i.incident_type = 'threshold' ORDER BY h.bucket", (inc_id,))


@router.get("/{inc_id}/notifications")
async def incident_notifications(inc_id: IncidentId, _user: Principal = Depends(current_user)):
    return await db.all(
        "SELECT n.*, u.full_name FROM notification n JOIN users u ON u.id = n.user_id WHERE n.incident_id = %s"
        " ORDER BY n.created_at, u.full_name, n.channel", (inc_id,))


async def _act(inc_id: int, user: Principal, sql: str, params: tuple) -> dict:
    """Процедура життєвого циклу від імені користувача; повертає оновлений інцидент."""
    try:
        async with db.tx(user_id=user.id) as conn:
            await conn.execute(sql, params)
            cur = await conn.execute("SELECT * FROM v_incident_list WHERE id = %s", (inc_id,))
            return await cur.fetchone()
    except psycopg.errors.NoDataFound:          # процедура не знайшла інцидент
        raise HTTPException(404, "Інцидент не знайдено") from None


@router.post("/{inc_id}/acknowledge")
async def acknowledge(inc_id: IncidentId, user: Principal = Depends(current_user)):
    return await _act(inc_id, user, "CALL sp_acknowledge_incident(%s, %s)", (inc_id, user.id))


@router.post("/{inc_id}/resolve")
async def resolve(inc_id: IncidentId, user: Principal = Depends(current_user)):
    return await _act(inc_id, user, "CALL sp_resolve_incident(%s, %s)", (inc_id, user.id))


@router.post("/{inc_id}/close")
async def close(inc_id: IncidentId, body: CloseIn, user: Principal = Depends(current_user)):
    """Закриття усунутого інциденту; порожній коментар відхиляє обмеження ck_incident_closed."""
    return await _act(inc_id, user, "CALL sp_close_incident(%s, %s, %s)", (inc_id, user.id, body.comment))

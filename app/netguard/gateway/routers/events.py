"""Журнал подій (FR-09): зміни стану обладнання, інциденти, дії користувачів, зміни конфігурації, автентифікація."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ...auth import Principal
from ..deps import current_user, db

router = APIRouter(prefix="/api/events", tags=["events"])

EventKind = Literal["status_change", "incident", "user_action", "config_change", "auth"]


@router.get("")
async def events(date_from: date = Query(alias="from"), date_to: date = Query(alias="to"),
                 event_type: EventKind | None = Query(None, alias="type"), location_id: int | None = None,
                 q: str = Query("", max_length=200), _user: Principal = Depends(current_user)):
    """Події за дати from…to включно, найновіші першими; не більше 1000."""
    return await db.all(
        "SELECT ev.*, u.full_name, e.name AS equipment, l.name AS location FROM event_log ev"
        " LEFT JOIN users u ON u.id = ev.user_id LEFT JOIN equipment e ON e.id = ev.equipment_id"
        " LEFT JOIN locations l ON l.id = e.location_id"
        " WHERE ev.created_at >= %(f)s::date AND ev.created_at < %(t)s::date + 1"
        "   AND (%(type)s::text IS NULL OR ev.event_type::text = %(type)s)"
        "   AND (%(loc)s::int IS NULL OR e.location_id = %(loc)s) AND ev.message ILIKE %(q)s"
        " ORDER BY ev.created_at DESC, ev.id DESC LIMIT 1000",
        {"f": date_from, "t": date_to, "type": event_type, "loc": location_id, "q": f"%{q}%"})

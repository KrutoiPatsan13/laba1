"""Сповіщення користувача (FR-07): лічильник у заголовку та сторінка «Сповіщення»."""
from fastapi import APIRouter, Depends

from ...auth import Principal
from ..deps import current_user, db

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/unread-count")
async def unread_count(user: Principal = Depends(current_user)):
    row = await db.one("SELECT count(*) AS n FROM notification WHERE user_id = %s AND channel = 'web'"
                       " AND created_at > now() - INTERVAL '24 hours'", (user.id,))
    return {"count": row["n"]}


@router.get("")
async def my_notifications(user: Principal = Depends(current_user)):
    """Останні 300 сповіщень поточного користувача всіма каналами з даними інцидентів."""
    return await db.all(
        "SELECT n.*, i.equipment, i.location, i.metric_name, i.metric_value, i.unit, i.incident_type, i.severity,"
        " i.status AS inc_status FROM notification n JOIN v_incident_list i ON i.id = n.incident_id"
        " WHERE n.user_id = %s ORDER BY n.created_at DESC LIMIT 300", (user.id,))

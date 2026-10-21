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

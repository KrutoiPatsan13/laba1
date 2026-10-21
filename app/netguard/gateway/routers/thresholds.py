"""Порогові значення метрик (FR-06): перелік із кількістю інцидентів, додавання, зміна, видалення.

Усі дії — лише для адміністратора; зміни фіксує тригер аудиту threshold_audit від імені
користувача з токена. Поріг, на який посилаються інциденти, видалити не дає обмеження
fk_incident_threshold — його деактивують.
"""
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from ...auth import Principal
from ..deps import admin_user, db

router = APIRouter(prefix="/api/thresholds", tags=["thresholds"])

ThresholdId = Annotated[int, Path(ge=1, le=2**31 - 1)]


class ThresholdIn(BaseModel):
    equipment_type_id: int = Field(ge=1, le=32767)
    metric_id: int = Field(ge=1, le=32767)
    severity: Literal["warning", "critical"] = "warning"
    min_value: Decimal | None = None        # NUMERIC(12,3); NaN і нескінченність pydantic відхиляє
    max_value: Decimal | None = None
    is_active: bool = True


@router.get("")
async def threshold_list(_user: Principal = Depends(admin_user)):
    return await db.all(
        "SELECT t.*, et.name AS type_name, m.name AS metric_name, m.unit,"
        " (SELECT count(*) FROM incident i WHERE i.threshold_id = t.id) AS incidents"
        " FROM threshold t JOIN equipment_type et ON et.id = t.equipment_type_id JOIN metric m ON m.id = t.metric_id"
        " ORDER BY et.name, m.id, t.severity")


@router.get("/reference")
async def reference(_user: Principal = Depends(admin_user)):
    """Типи обладнання (без службових «~…») і метрики для форми порогу."""
    return {"types": await db.all("SELECT id, name, category FROM equipment_type WHERE name NOT LIKE '~%%' ORDER BY name"),
            "metrics": await db.all("SELECT id, name, unit FROM metric ORDER BY id")}


@router.post("", status_code=201)
async def create_threshold(body: ThresholdIn, user: Principal = Depends(admin_user)):
    return await db.run(
        "INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value, max_value, is_active)"
        " VALUES (%(equipment_type_id)s, %(metric_id)s, %(severity)s, %(min_value)s, %(max_value)s, %(is_active)s)"
        " RETURNING id", body.model_dump(), user_id=user.id)


@router.put("/{th_id}")
async def update_threshold(th_id: ThresholdId, body: ThresholdIn, user: Principal = Depends(admin_user)):
    row = await db.run(
        "UPDATE threshold SET equipment_type_id = %(equipment_type_id)s, metric_id = %(metric_id)s,"
        " severity = %(severity)s, min_value = %(min_value)s, max_value = %(max_value)s, is_active = %(is_active)s"
        " WHERE id = %(id)s RETURNING id", body.model_dump() | {"id": th_id}, user_id=user.id)
    if not row:
        raise HTTPException(404, "Поріг не знайдено")
    return row


@router.delete("/{th_id}")
async def delete_threshold(th_id: ThresholdId, user: Principal = Depends(admin_user)):
    row = await db.run("DELETE FROM threshold WHERE id = %s RETURNING id", (th_id,), user_id=user.id)
    if not row:
        raise HTTPException(404, "Поріг не знайдено")
    return row

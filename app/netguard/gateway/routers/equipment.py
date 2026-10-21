"""Панель моніторингу (FR-05), облік обладнання та ДБЖ (FR-02, FR-03), історія показників (FR-12).

Стан і картки обладнання бачать обидві ролі; додавати, редагувати, переводити в обслуговування
й виводити з експлуатації може лише адміністратор.
"""
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...auth import Principal
from ..deps import admin_user, current_user, db

router = APIRouter(prefix="/api", tags=["equipment"])

Category = Literal["ups", "switch", "router", "onu"]

# період графіків → (тривалість, інтервал агрегування fn_metric_history)
PERIODS = {"6h": ("6 hours", "2 minutes"), "24h": ("1 day", "10 minutes"), "7d": ("7 days", "1 hour")}

# дія з картки пристрою → зміна в equipment (фіксований перелік, текст SQL із запиту не надходить)
STATE_ACTIONS = {"maintenance": "status = 'maintenance'",
                 "end_maintenance": "status = CASE WHEN status = 'maintenance' THEN 'unknown' ELSE status END",
                 "retire": "is_active = FALSE",
                 "restore": "is_active = TRUE"}


class EquipmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ip: str = Field(min_length=1, max_length=64)
    type_id: int
    location_id: int
    credential_id: int
    snmp_port: int = 161
    poll_interval_s: int = 60
    model: str | None = Field(None, max_length=100)
    serial_number: str | None = Field(None, max_length=50)
    installed_on: date | None = None


class UpsIn(BaseModel):
    """Параметри функції БД fn_register_ups: обладнання + паспорт ДБЖ."""
    type_id: int
    location_id: int
    name: str = Field(min_length=1, max_length=100)
    ip: str = Field(min_length=1, max_length=64)
    credential_id: int
    rated_power_va: int
    battery_capacity_ah: Decimal
    rated_power_w: int | None = None
    battery_count: int = 1
    nominal_voltage_v: Decimal | None = None
    model: str | None = Field(None, max_length=100)
    battery_installed_on: date | None = None


class StateChange(BaseModel):
    action: Literal["maintenance", "end_maintenance", "retire", "restore"]


@router.get("/dashboard")
async def dashboard(_user: Principal = Depends(current_user)):
    summary = await db.one(
        "SELECT count(*) FILTER (WHERE is_active) total,"
        " count(*) FILTER (WHERE is_active AND status = 'online') online,"
        " count(*) FILTER (WHERE is_active AND status = 'offline') offline,"
        " (SELECT count(*) FROM incident WHERE status IN ('new', 'escalated', 'acknowledged')) open_inc,"
        " (SELECT count(*) FROM v_incident_list WHERE status IN ('new', 'escalated', 'acknowledged')"
        "                                         AND severity = 'critical') critical,"
        " (SELECT count(*) FROM incident WHERE status = 'escalated') escalated,"
        " (SELECT min((last_values ->> 'battery_charge')::NUMERIC) FROM v_equipment_current_state"
        "   WHERE category = 'ups' AND is_active) min_charge"
        " FROM equipment")
    devices = await db.all("SELECT * FROM v_equipment_current_state WHERE is_active ORDER BY location, name")
    incidents = await db.all(
        "SELECT * FROM v_incident_list WHERE status IN ('new', 'escalated', 'acknowledged') ORDER BY opened_at DESC LIMIT 8")
    return {"summary": summary, "devices": devices, "incidents": incidents}


@router.get("/reference")
async def reference(_user: Principal = Depends(current_user)):
    """Довідники для фільтрів і форм обладнання."""
    return {"locations": await db.all("SELECT id, name FROM locations ORDER BY name"),
            "types": await db.all("SELECT id, name, category FROM equipment_type WHERE name NOT LIKE '~%%' ORDER BY name"),
            "credentials": await db.all("SELECT id, name, snmp_version FROM snmp_credential ORDER BY name")}


@router.get("/equipment")
async def equipment_list(location_id: int | None = None, category: Category | None = None,
                         status: Literal["online", "offline", "unknown", "maintenance", "retired"] | None = None,
                         q: str = Query("", max_length=100), _user: Principal = Depends(current_user)):
    return await db.all(
        "SELECT s.*, e.poll_interval_s, e.config_id, e.model FROM v_equipment_current_state s"
        " JOIN equipment e ON e.id = s.id"
        " WHERE (%(loc)s::int IS NULL OR s.location_id = %(loc)s) AND (%(cat)s::text IS NULL OR s.category::text = %(cat)s)"
        "   AND (%(st)s::text IS NULL OR (CASE WHEN s.is_active THEN s.status::text ELSE 'retired' END) = %(st)s)"
        "   AND s.name ILIKE %(q)s ORDER BY s.location, s.name",
        {"loc": location_id, "cat": category, "st": status, "q": f"%{q}%"})


@router.post("/equipment", status_code=201)
async def create_equipment(body: EquipmentIn, user: Principal = Depends(admin_user)):
    return await db.run(
        "INSERT INTO equipment (name, ip_address, type_id, location_id, credential_id, snmp_port, poll_interval_s,"
        " model, serial_number, installed_on) VALUES (%(name)s, %(ip)s, %(type_id)s, %(location_id)s, %(credential_id)s,"
        " %(snmp_port)s, %(poll_interval_s)s, %(model)s, %(serial_number)s, %(installed_on)s) RETURNING id",
        body.model_dump(), user_id=user.id)


@router.post("/equipment/ups", status_code=201)
async def register_ups(body: UpsIn, user: Principal = Depends(admin_user)):
    """Реєстрація ДБЖ однією транзакцією (функція fn_register_ups)."""
    return await db.run(
        "SELECT fn_register_ups(%(type_id)s::smallint, %(location_id)s, %(name)s, %(ip)s::inet, %(credential_id)s::smallint,"
        " %(rated_power_va)s, %(battery_capacity_ah)s, %(rated_power_w)s, %(battery_count)s::smallint,"
        " %(nominal_voltage_v)s, %(model)s, NULL, %(battery_installed_on)s) AS id", body.model_dump(), user_id=user.id)


@router.get("/equipment/{eq_id}")
async def equipment_card(eq_id: int, _user: Principal = Depends(current_user)):
    eq = await db.one(
        "SELECT e.*, host(e.ip_address) AS ip, t.name AS type_name, t.category, t.snmp_profile, l.name AS location,"
        " c.name AS credential, c.snmp_version, u.rated_power_va, u.rated_power_w, u.battery_capacity_ah,"
        " u.battery_count, u.nominal_voltage_v, u.battery_installed_on"
        " FROM equipment e JOIN equipment_type t ON t.id = e.type_id JOIN locations l ON l.id = e.location_id"
        " JOIN snmp_credential c ON c.id = e.credential_id LEFT JOIN ups_details u ON u.equipment_id = e.id"
        " WHERE e.id = %s", (eq_id,))
    if not eq:
        raise HTTPException(404, "Обладнання не знайдено")
    return eq


@router.put("/equipment/{eq_id}")
async def update_equipment(eq_id: int, body: EquipmentIn, user: Principal = Depends(admin_user)):
    row = await db.run(
        "UPDATE equipment SET name = %(name)s, ip_address = %(ip)s, type_id = %(type_id)s, location_id = %(location_id)s,"
        " credential_id = %(credential_id)s, snmp_port = %(snmp_port)s, poll_interval_s = %(poll_interval_s)s,"
        " model = %(model)s, serial_number = %(serial_number)s, installed_on = %(installed_on)s"
        " WHERE id = %(id)s RETURNING id", body.model_dump() | {"id": eq_id}, user_id=user.id)
    if not row:
        raise HTTPException(404, "Обладнання не знайдено")
    return row


@router.patch("/equipment/{eq_id}/state")
async def change_state(eq_id: int, body: StateChange, user: Principal = Depends(admin_user)):
    """Обслуговування, завершення обслуговування, виведення з експлуатації та повернення в неї."""
    row = await db.run(f"UPDATE equipment SET {STATE_ACTIONS[body.action]} WHERE id = %s RETURNING id, status, is_active",
                       (eq_id,), user_id=user.id)
    if not row:
        raise HTTPException(404, "Обладнання не знайдено")
    return row


@router.get("/equipment/{eq_id}/history")
async def history(eq_id: int, period: Literal["6h", "24h", "7d"] = "24h", _user: Principal = Depends(current_user)):
    """Показники типу обладнання з історією за період і активні пороги для позначок на графіках."""
    span, bucket = PERIODS[period]
    metrics = await db.all(
        "SELECT m.code, m.name, m.unit,"
        " (SELECT coalesce(jsonb_agg(h ORDER BY h.bucket), '[]')"
        "    FROM fn_metric_history(e.id, m.code, now() - %(span)s::interval, now(), %(bucket)s::interval) h) AS history"
        " FROM equipment e JOIN snmp_oid_map om ON om.equipment_type_id = e.type_id JOIN metric m ON m.id = om.metric_id"
        " WHERE e.id = %(id)s ORDER BY m.id", {"id": eq_id, "span": span, "bucket": bucket})
    thresholds = await db.all(
        "SELECT m.code, t.severity, t.min_value, t.max_value FROM threshold t JOIN metric m ON m.id = t.metric_id"
        " JOIN equipment e ON e.type_id = t.equipment_type_id WHERE e.id = %s AND t.is_active", (eq_id,))
    return {"metrics": metrics, "thresholds": thresholds}


@router.get("/equipment/{eq_id}/incidents")
async def equipment_incidents(eq_id: int, _user: Principal = Depends(current_user)):
    return await db.all("SELECT * FROM v_incident_list WHERE equipment_id = %s ORDER BY opened_at DESC LIMIT 20", (eq_id,))

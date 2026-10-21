"""Конфігурація SNMP (ЛР4 ТСПП): застосування YAML-файлу, активна конфігурація, історія версій.

Файл надходить тілом запиту, ім'я — у заголовку X-Filename (UTF-8 у percent-encoding). API Gateway
суворо перевіряє файл модулем netguard.snmp_yaml, а застосовує його однією транзакцією функція БД
sp_apply_snmp_config: помилка у файлі чи в БД не змінює активну версію. Усі дії — лише для адміністратора.
"""
import asyncio
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from psycopg.types.json import Jsonb

from ...auth import Principal
from ...snmp_yaml import ConfigError, load
from ..deps import admin_user, db

router = APIRouter(prefix="/api/snmp", tags=["snmp"])

MAX_SIZE = 1024 * 1024          # найбільший прийнятний файл, байт
ConfigId = Annotated[int, Path(ge=1, le=2**31 - 1)]


@router.get("/active")
async def active(_user: Principal = Depends(admin_user)):
    """Активна версія та її наслідок у БД: облікові дані SNMP і шаблони опитування (метрика → OID)."""
    return {"config": await db.one("SELECT * FROM snmp_config WHERE is_active"),
            "credentials": await db.all(
                "SELECT name, snmp_version, username, auth_protocol, priv_protocol, secret_ref,"
                " (SELECT count(*) FROM equipment e WHERE e.credential_id = c.id AND e.is_active) AS devices"
                " FROM snmp_credential c ORDER BY name"),
            "plan": await db.all(
                "SELECT t.snmp_profile, t.name, m.code, om.oid, om.scale FROM snmp_oid_map om"
                " JOIN equipment_type t ON t.id = om.equipment_type_id JOIN metric m ON m.id = om.metric_id"
                " ORDER BY t.snmp_profile, m.id")}


@router.get("/versions")
async def versions(_user: Principal = Depends(admin_user)):
    """Історія версій, найновіші першими, з кількістю пристроїв у файлі й синхронізованих версією."""
    return await db.all(
        "SELECT c.*, u.full_name, (SELECT count(*) FROM equipment e WHERE e.config_id = c.id) AS devices,"
        " jsonb_array_length(c.content -> 'devices') AS in_file FROM snmp_config c"
        " LEFT JOIN users u ON u.id = c.loaded_by ORDER BY c.id DESC")


@router.post("/versions")
async def upload(request: Request, user: Principal = Depends(admin_user)):
    """Новий YAML-файл; результат — skipped (ця версія вже активна), reactivated або applied."""
    name = PurePosixPath(unquote(request.headers.get("x-filename", ""))).name
    if not name.lower().endswith((".yaml", ".yml")):
        raise HTTPException(422, "Потрібен файл .yaml або .yml")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_SIZE:
            raise HTTPException(413, f"Файл {name} завеликий: найбільший розмір — {MAX_SIZE // 1024} КБ")
    try:
        cfg, sha = await asyncio.to_thread(load, bytes(raw))
    except ConfigError as e:
        raise HTTPException(422, f"Помилка у файлі {name}: {e}") from None
    row = await db.run("SELECT sp_apply_snmp_config(%s, %s, %s, %s) AS r",
                       (f"/etc/netguard/{name}", sha, Jsonb(cfg), user.id), user_id=user.id)
    return {"result": row["r"]}


@router.post("/versions/{cfg_id}/activate")
async def activate(cfg_id: ConfigId, user: Principal = Depends(admin_user)):
    """Повернення до раніше застосованої версії з її збереженим вмістом."""
    row = await db.run("SELECT sp_apply_snmp_config(file_path, sha256, content, %s) AS r FROM snmp_config WHERE id = %s",
                       (user.id, cfg_id), user_id=user.id)
    if not row:
        raise HTTPException(404, "Версію конфігурації не знайдено")
    return {"result": row["r"]}

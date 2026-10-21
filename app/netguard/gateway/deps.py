"""Спільне для маршрутів API Gateway: пул з'єднань з БД (роль ng_web), користувач запиту,
клієнт Reporting Service."""
import httpx
from fastapi import Depends, HTTPException, Request

from ..auth import Principal
from ..db import Database
from ..service_kit import token_principal

db = Database("ng_web")
reporting: httpx.AsyncClient | None = None      # створюється під час старту застосунку


async def current_user(request: Request) -> Principal:
    """Користувач із токена; роль і блокування перевіряються в БД у кожному запиті, тож
    заблокований користувач втрачає доступ одразу, а не після закінчення строку токена."""
    p = token_principal(request)
    row = await db.one("SELECT u.full_name, u.is_active, r.code AS role FROM users u JOIN roles r ON r.id = u.role_id"
                       " WHERE u.id = %s", (p.id,))
    if not row or not row["is_active"]:
        raise HTTPException(401, "Обліковий запис заблоковано або видалено", headers={"WWW-Authenticate": "Bearer"})
    return Principal(p.id, row["full_name"], row["role"])


async def admin_user(user: Principal = Depends(current_user)) -> Principal:
    if not user.is_admin:
        raise HTTPException(403, "Дія доступна лише адміністратору")
    return user

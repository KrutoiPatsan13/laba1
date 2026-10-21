"""Користувачі (FR-11): облікові записи, ролі, контакти для сповіщень, паролі, блокування.

Усі дії — лише для адміністратора; пароль хешує функція БД fn_hash_password (bcrypt), хеш
із API не повертається. Зміни фіксує тригер аудиту users_audit від імені користувача з токена.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from ...auth import Principal
from ..deps import admin_user, db

router = APIRouter(prefix="/api/users", tags=["users"])

UserId = Annotated[int, Path(ge=1, le=2**31 - 1)]


class UserIn(BaseModel):
    """Обмеження логіна й e-mail (ck_users_login, ck_users_email, унікальність) перевіряє БД."""
    full_name: str = Field(min_length=1, max_length=150)
    login: str = Field(min_length=1, max_length=50)
    role_id: int = Field(ge=1, le=32767)
    email: str | None = Field(None, max_length=254)
    telegram_chat_id: int | None = Field(None, ge=-2**63, le=2**63 - 1)
    is_active: bool = True
    password: str | None = Field(None, min_length=8, max_length=200)    # під час зміни: None — не змінювати


@router.get("")
async def user_list(_user: Principal = Depends(admin_user)):
    return await db.all(
        "SELECT u.id, u.role_id, u.full_name, u.login, u.email, u.telegram_chat_id, u.is_active, u.created_at,"
        " u.last_login_at, r.code AS role_code FROM users u JOIN roles r ON r.id = u.role_id ORDER BY u.full_name")


@router.get("/roles")
async def roles(_user: Principal = Depends(admin_user)):
    return await db.all("SELECT id, name FROM roles ORDER BY id")


@router.post("", status_code=201)
async def create_user(body: UserIn, user: Principal = Depends(admin_user)):
    if body.password is None:
        raise HTTPException(422, "Пароль має містити щонайменше 8 символів")
    return await db.run(
        "INSERT INTO users (full_name, login, role_id, email, telegram_chat_id, is_active, password_hash)"
        " VALUES (%(full_name)s, %(login)s, %(role_id)s, %(email)s, %(telegram_chat_id)s, %(is_active)s,"
        " fn_hash_password(%(password)s)) RETURNING id", body.model_dump(), user_id=user.id)


@router.put("/{user_id}")
async def update_user(user_id: UserId, body: UserIn, user: Principal = Depends(admin_user)):
    if user_id == user.id and not body.is_active:
        raise HTTPException(409, "Не можна заблокувати власний обліковий запис")
    row = await db.run(
        "UPDATE users SET full_name = %(full_name)s, login = %(login)s, role_id = %(role_id)s, email = %(email)s,"
        " telegram_chat_id = %(telegram_chat_id)s, is_active = %(is_active)s,"
        " password_hash = coalesce(fn_hash_password(%(password)s), password_hash) WHERE id = %(id)s RETURNING id",
        body.model_dump() | {"id": user_id}, user_id=user.id)
    if not row:
        raise HTTPException(404, "Користувача не знайдено")
    return row

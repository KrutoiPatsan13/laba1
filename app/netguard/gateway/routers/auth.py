"""Автентифікація (FR-01): вхід за логіном і паролем, дані поточного користувача."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...auth import Principal, issue
from ..deps import current_user, db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    login: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
async def login(body: Credentials):
    # fn_authenticate пише подію auth у журнал і оновлює момент останнього входу
    row = await db.run("SELECT * FROM fn_authenticate(%s, %s)", (body.login.strip().lower(), body.password))
    if not row:
        raise HTTPException(401, "Невірний логін або пароль, або обліковий запис заблоковано")
    return {"token": issue(row["user_id"], row["full_name"], row["role_code"]),
            "user": {"id": row["user_id"], "name": row["full_name"], "role": row["role_code"]}}


@router.get("/me")
async def me(user: Principal = Depends(current_user)):
    return {"id": user.id, "name": user.name, "role": user.role}

"""Токени доступу (JWT, HS256).

Токен видає API Gateway / Auth Service після перевірки пароля функцією БД fn_authenticate;
API Gateway і Reporting Service перевіряють підпис і строк дії в кожному запиті. Секрет
NETGUARD_JWT_SECRET спільний для цих сервісів і більше нікому не відомий.
"""
import time
from dataclasses import dataclass

import jwt

from .settings import settings

ALGORITHM = "HS256"
ISSUER = "netguard-gateway"


@dataclass(frozen=True)
class Principal:
    """Автентифікований користувач, від імені якого виконується запит."""
    id: int
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def issue(user_id: int, name: str, role: str) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "name": name, "role": role, "iss": ISSUER, "iat": now,
              "exp": now + settings.jwt_ttl_min * 60}
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def verify(token: str) -> Principal:
    """Перевіряє підпис, видавця й строк дії; помилка — jwt.InvalidTokenError."""
    c = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM], issuer=ISSUER,
                   options={"require": ["sub", "exp", "iss"]})
    return Principal(int(c["sub"]), c.get("name", ""), c.get("role", ""))

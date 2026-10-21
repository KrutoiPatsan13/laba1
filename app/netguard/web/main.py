"""Веб-інтерфейс NetGuard Monitor (API Gateway + інтерфейс користувача).

Інтерфейс описано мовою Python засобами NiceGUI: бібліотека перетворює дерево
Python-об'єктів на веб-сторінки (Vue/Quasar у браузері) й синхронізує їх через
WebSocket, а HTTP-частину обслуговує FastAPI. Доступ до сторінок — лише після
входу; права ролей «Сапорт» і «Адміністратор» перевіряються на кожній сторінці.

    python -m netguard.web
"""
from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from ..db import error_text
from ..settings import settings
from . import pages_admin, pages_incidents, pages_monitoring, pages_queries, pages_reports  # noqa: F401 (реєстрація сторінок)
from .common import db

PUBLIC = {"/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """Неавтентифікований користувач бачить лише сторінку входу."""

    async def dispatch(self, request: Request, call_next):
        if not app.storage.user.get("authenticated"):
            path = request.url.path
            if not path.startswith("/_nicegui") and path not in PUBLIC:
                return RedirectResponse(f"/login?redirect_to={path}")
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.on_startup(db.open)
app.on_shutdown(db.close)


@ui.page("/login")
def login_page(redirect_to: str = "/"):
    if app.storage.user.get("authenticated"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#1d3557")
    ui.query("body").style("background: linear-gradient(135deg, #1d3557 0%, #457b9d 100%)")

    async def try_login():
        if not login.value or not password.value:
            ui.notify("Введіть логін і пароль", type="warning")
            return
        try:
            row = await db.run("SELECT * FROM fn_authenticate(%s, %s)", (login.value.strip(), password.value))
        except Exception as e:
            ui.notify(f"Помилка БД: {error_text(e)}", type="negative")
            return
        if not row:
            ui.notify("Невірний логін або пароль, або обліковий запис заблоковано", type="negative")
            password.value = ""
            return
        app.storage.user.update(id=row["user_id"], name=row["full_name"], role=row["role_code"], authenticated=True)
        ui.navigate.to(redirect_to if redirect_to.startswith("/") else "/")

    with ui.column().classes("absolute-center items-center gap-4"):
        with ui.row().classes("items-center gap-3 text-white"):
            ui.icon("lan").classes("text-5xl")
            with ui.column().classes("gap-0"):
                ui.label("NetGuard Monitor").classes("text-3xl font-bold")
                ui.label("Моніторинг мережевого обладнання та ДБЖ").classes("text-sm opacity-80")
        with ui.card().classes("w-96 p-6 gap-3"):
            ui.label("Вхід до системи").classes("text-lg font-semibold text-grey-9")
            login = ui.input("Логін").props("outlined autofocus").classes("w-full")
            password = ui.input("Пароль", password=True, password_toggle_button=True).props("outlined").classes("w-full")
            password.on("keydown.enter", try_login)
            ui.button("Увійти", icon="login", on_click=try_login).classes("w-full").props("unelevated size=lg")
            ui.label("Доступ лише для інженерів технічної підтримки та адміністраторів провайдера") \
                .classes("text-xs text-grey-6 text-center w-full")


def main():
    ui.run(title="NetGuard Monitor", port=settings.web_port, storage_secret=settings.storage_secret,
           language="uk", favicon="🛰", reload=False, show=False, show_welcome_message=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()

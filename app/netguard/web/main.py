"""Web UI — мікросервіс веб-інтерфейсу NetGuard Monitor.

Інтерфейс описано мовою Python засобами NiceGUI: бібліотека перетворює дерево
Python-об'єктів на веб-сторінки (Vue/Quasar у браузері) й синхронізує їх через
WebSocket. До БД сервіс не підключається: дані він отримує з API Gateway за REST
(модуль api) з токеном користувача, який видає API Gateway / Auth Service під час входу.
Меню й сторінки адміністратора приховано для ролі «Сапорт», а права ролей ще раз
перевіряє API Gateway у кожному запиті.

    python -m netguard.web                # http://127.0.0.1:8080
"""
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware

from ..settings import require_secret, settings
from . import api

PUBLIC = {"/login"}


def local_path(target: str) -> str:
    """Адреса для переходу після входу — лише шлях цього сайту (без схеми й чужого хоста)."""
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class AuthMiddleware(BaseHTTPMiddleware):
    """Сторінки — лише з дійсним токеном. Перед кожною сторінкою токен перевіряє API Gateway,
    тож прострочений токен або заблокований користувач потрапляють на сторінку входу."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/_nicegui") or path in PUBLIC:
            return await call_next(request)
        token = app.storage.user.get("token")
        if token:
            try:
                r = await api.client().get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 401:
                    app.storage.user.clear()
                    token = None
            except httpx.HTTPError:
                pass                        # шлюз недоступний — сторінка сама покаже помилку
        if not token:
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/login?" + urlencode({"redirect_to": target}))
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.on_shutdown(api.close)


@ui.page("/login")
def login_page(redirect_to: str = "/"):
    if app.storage.user.get("token"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#1d3557")
    ui.query("body").style("background: linear-gradient(135deg, #1d3557 0%, #457b9d 100%)")

    async def try_login():
        if not login.value or not password.value:
            ui.notify("Введіть логін і пароль", type="warning")
            return
        try:
            res = await api.post("/api/auth/login", {"login": login.value.strip(), "password": password.value}, auth=False)
        except api.ApiError as e:
            ui.notify(e.message, type="negative")
            if e.status == 401:
                password.value = ""
            return
        u = res["user"]
        app.storage.user.update(token=res["token"], id=u["id"], name=u["name"], role=u["role"], authenticated=True)
        ui.navigate.to(local_path(redirect_to))

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


def run():
    require_secret("NETGUARD_STORAGE_SECRET", settings.storage_secret)
    ui.run(title="NetGuard Monitor", host=settings.bind_host, port=settings.web_port, storage_secret=settings.storage_secret,
           language="uk", favicon="🛰", reload=False, show=False, show_welcome_message=False)


def main():
    # сторінки реєструються декораторами @ui.page під час імпорту модулів
    from . import pages_admin, pages_incidents, pages_monitoring, pages_queries, pages_reports  # noqa: F401
    run()


if __name__ in {"__main__", "__mp_main__"}:
    main()

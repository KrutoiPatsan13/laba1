"""Спільні елементи веб-інтерфейсу: каркас сторінки, меню за ролями, довідники підписів,
форматування значень, що надходять від API Gateway у JSON."""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from zoneinfo import ZoneInfo

from nicegui import app, ui

from . import api

KYIV = ZoneInfo("Europe/Kyiv")     # часовий пояс провайдера: у ньому БД рахує доби, звіти й межі фільтрів

EQ_STATUS = {"online": ("Онлайн", "positive"), "offline": ("Офлайн", "negative"),
             "unknown": ("Невідомо", "grey-6"), "maintenance": ("Обслуговування", "warning")}
INC_STATUS = {"new": ("Новий", "negative"), "escalated": ("Ескальовано", "deep-orange"),
              "acknowledged": ("Прийнято", "amber-8"), "resolved": ("Усунено", "info"), "closed": ("Закрито", "positive")}
SEVERITY = {"critical": ("Критично", "negative"), "warning": ("Попередження", "warning"), None: ("Недоступність", "grey-8")}
CATEGORY = {"ups": "ДБЖ", "switch": "Комутатор", "router": "Маршрутизатор", "onu": "ONU"}
EVENT = {"status_change": ("Стан обладнання", "info"), "incident": ("Інцидент", "negative"),
         "user_action": ("Дія користувача", "secondary"), "config_change": ("Конфігурація", "accent"),
         "auth": ("Автентифікація", "grey-7")}
CHANNEL = {"web": "Веб-панель", "email": "E-mail", "telegram": "Telegram"}
NOTIFY_STATUS = {"queued": ("У черзі", "grey-6"), "sent": ("Надіслано", "positive"), "failed": ("Помилка", "negative")}
ROLE = {"admin": "Адміністратор", "support": "Сапорт"}

NAV = [  # значок, підпис, шлях, лише для адміністратора
    ("dashboard", "Панель моніторингу", "/", False),
    ("router", "Обладнання", "/equipment", False),
    ("warning", "Інциденти", "/incidents", False),
    ("notifications", "Сповіщення", "/notifications", False),
    ("history", "Журнал подій", "/events", False),
    ("assessment", "Звіти", "/reports", False),
    ("tune", "Порогові значення", "/thresholds", True),
    ("settings_ethernet", "Конфігурація SNMP", "/snmp", True),
    ("group", "Користувачі", "/users", True),
    ("storage", "Запити до БД", "/queries", True),
]


def user() -> dict:
    return app.storage.user


def is_admin() -> bool:
    return app.storage.user.get("role") == "admin"


def uid() -> int:
    return app.storage.user["id"]


ISO_DT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def dt(v) -> datetime | date | None:
    """Момент часу з JSON (рядок ISO 8601) → datetime/date; інші значення — без змін."""
    if isinstance(v, str):
        if ISO_DT.fullmatch(v):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        if ISO_DATE.fullmatch(v):
            return date.fromisoformat(v)
    return v


def fmt_dt(v, seconds=False) -> str:
    if v is None:
        return "—"
    v = dt(v)
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return str(v)


def fmt_td(v: timedelta | float | int | None) -> str:
    """Тривалість: timedelta або кількість секунд (так API передає INTERVAL)."""
    if v is None:
        return "—"
    s = int(v.total_seconds() if isinstance(v, timedelta) else float(v))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = ([f"{d} д"] if d else []) + ([f"{h} год"] if h else []) + ([f"{m} хв"] if m else [])
    return " ".join(parts) or f"{s} с"


def fmt_num(v, digits=1) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def jsonable(value):
    """Значення з відповіді API → дані для таблиць NiceGUI: моменти часу у вигляді «дд.мм.рррр гг:хв»."""
    if isinstance(value, str) and (ISO_DT.fullmatch(value) or ISO_DATE.fullmatch(value)):
        return fmt_dt(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return fmt_dt(value)
    if isinstance(value, date):
        return fmt_dt(value)
    if isinstance(value, timedelta):
        return fmt_td(value)
    if isinstance(value, (IPv4Address, IPv6Address)):
        return str(value)
    return value


def badge(text: str, color: str):
    return ui.badge(text, color=color).classes("text-sm px-2 py-1")


def badge_slot(table, column: str):
    """Колонка таблиці як кольоровий значок: у рядку потрібні поля <column>_label і <column>_color."""
    table.add_slot(f"body-cell-{column}", f"""
        <q-td :props="props">
          <q-badge :color="props.row.{column}_color" :label="props.row.{column}_label" class="text-sm px-2 py-1"/>
        </q-td>""")


async def frame(title: str, subtitle: str | None = None):
    """Каркас сторінки: шапка, меню за роллю, заголовок. Повертає колонку для вмісту."""
    ui.colors(primary="#1d3557", secondary="#457b9d", accent="#7b2cbf", positive="#2a9d8f",
              negative="#d62828", warning="#f4a261", info="#4d7ea8")
    ui.query("body").style("background-color: #f4f6f9")
    try:
        unread = (await api.get("/api/notifications/unread-count"))["count"]
    except api.ApiError:
        unread = 0
    u = user()          # після відповіді 401 сеанс уже очищено, а перехід на /login поставлено в чергу
    with ui.header().classes("items-center justify-between bg-primary px-4 py-2"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round color=white")
            ui.icon("lan").classes("text-3xl")
            ui.label("NetGuard Monitor").classes("text-xl font-bold tracking-wide")
            ui.label("моніторинг обладнання та ДБЖ").classes("text-sm opacity-70 max-sm:hidden")
        with ui.row().classes("items-center gap-3"):
            with ui.button(icon="notifications", on_click=lambda: ui.navigate.to("/notifications")) \
                    .props("flat round color=white"):
                if unread:
                    ui.badge(str(unread), color="negative").props("floating")
            with ui.column().classes("gap-0 items-end"):
                ui.label(u.get("name", "")).classes("text-sm font-medium")
                ui.label(ROLE.get(u.get("role"), "")).classes("text-xs opacity-70")
            ui.button(icon="logout", on_click=logout).props("flat round color=white").tooltip("Вийти")
    current = ui.context.client.page.path
    with ui.left_drawer(value=True, bordered=True).classes("bg-white").props("width=264") as drawer:
        with ui.list().props("padding").classes("w-full"):
            for icon, label, path, admin_only in NAV:
                if admin_only and not is_admin():
                    continue
                active = current == "/" if path == "/" else current.startswith(path)
                with ui.item(on_click=lambda p=path: ui.navigate.to(p)) \
                        .props("clickable" + (" active active-class='bg-blue-1 text-primary'" if active else "")) \
                        .classes("rounded-r-full mr-3"):
                    with ui.item_section().props("avatar").classes("min-w-0 pr-3"):
                        ui.icon(icon, color="primary").classes("text-xl")
                    with ui.item_section():
                        ui.item_label(label).classes("text-[15px] " + ("font-semibold" if active else ""))
    content = ui.column().classes("w-full p-6 gap-4")
    with content:
        with ui.column().classes("gap-0"):
            ui.label(title).classes("text-2xl font-semibold text-grey-9")
            if subtitle:
                ui.label(subtitle).classes("text-sm text-grey-7")
    return content


def logout():
    app.storage.user.clear()
    ui.navigate.to("/login")


def admin_only() -> bool:
    """Для сторінок адміністратора: повертає False і показує повідомлення, якщо роль не та."""
    if not is_admin():
        with ui.column().classes("w-full items-center p-10"):
            ui.icon("lock").classes("text-6xl text-grey-6")
            ui.label("Сторінка доступна лише адміністратору").classes("text-lg text-grey-8")
        return False
    return True


def stat_card(icon: str, label: str, value: str, color: str = "primary", hint: str | None = None):
    with ui.card().classes("min-w-[200px] flex-1 p-4"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.icon(icon).classes(f"text-4xl text-{color}")
            with ui.column().classes("gap-0"):
                ui.label(value).classes(f"text-2xl font-bold text-{color}")
                ui.label(label).classes("text-sm text-grey-8")
                if hint:
                    ui.label(hint).classes("text-xs text-grey-6")

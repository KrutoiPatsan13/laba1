"""Інциденти та їхній життєвий цикл (FR-07, FR-08), сповіщення користувача, журнал подій (FR-09)."""
import csv
import io
import json
from datetime import date, datetime, timedelta

from nicegui import ui

from ..db import error_text
from .common import (CHANNEL, EVENT, INC_STATUS, NOTIFY_STATUS, SEVERITY, badge, badge_slot, db, fmt_dt, fmt_num,
                     frame, uid)

OPEN = ("new", "escalated", "acknowledged")


def norm_text(lo, hi, unit) -> str:
    u = f" {unit}" if unit else ""
    if lo is not None and hi is not None:
        return f"{fmt_num(lo)} … {fmt_num(hi)}{u}"
    return f"≥ {fmt_num(lo)}{u}" if lo is not None else f"≤ {fmt_num(hi)}{u}"


def incident_row(i: dict) -> dict:
    sl, sc = INC_STATUS[i["status"]]
    vl, vc = SEVERITY[i["severity"]]
    what = "Немає відповіді на SNMP-запити" if i["incident_type"] == "offline" else \
        f"{i['metric_name']} = {fmt_num(i['metric_value'])} {i['unit'] or ''}"
    norm = "" if i["incident_type"] == "offline" else norm_text(i["min_value"], i["max_value"], i["unit"])
    return {"id": i["id"], "opened": fmt_dt(i["opened_at"]), "equipment": i["equipment"], "location": i["location"],
            "what": what, "norm": norm or "—", "status_label": sl, "status_color": sc, "severity_label": vl,
            "severity_color": vc, "ack": i["acknowledged_by_name"] or "—"}


INC_COLUMNS = [
    {"name": "id", "label": "№", "field": "id", "sortable": True},
    {"name": "opened", "label": "Відкрито", "field": "opened", "align": "left"},
    {"name": "equipment", "label": "Пристрій", "field": "equipment", "align": "left", "sortable": True},
    {"name": "location", "label": "Вузол", "field": "location", "align": "left", "sortable": True},
    {"name": "what", "label": "Аварія", "field": "what", "align": "left"},
    {"name": "norm", "label": "Норма", "field": "norm", "align": "left"},
    {"name": "severity", "label": "Рівень", "field": "severity_label", "align": "left"},
    {"name": "status", "label": "Статус", "field": "status_label", "align": "left"},
    {"name": "ack", "label": "Прийняв", "field": "ack", "align": "left"},
]


@ui.page("/incidents")
async def incidents_list(status: str = "open", days: int = 7):
    await frame("Інциденти", "Аварійні події: порушення порогових значень і недоступність обладнання")
    locs = {r["id"]: r["name"] for r in await db.all("SELECT id, name FROM locations ORDER BY name")}
    flt = {"status": status if status in ("open", "all", *INC_STATUS) else "open", "loc": None, "days": days}

    @ui.refreshable
    async def table():
        statuses = list(OPEN) if flt["status"] == "open" else list(INC_STATUS) if flt["status"] == "all" \
            else [flt["status"]]
        rows = await db.all(
            "SELECT * FROM v_incident_list WHERE status::text = ANY(%s) AND (%s::int IS NULL OR location_id = %s)"
            " AND opened_at > now() - make_interval(days => %s) ORDER BY opened_at DESC",
            (statuses, flt["loc"], flt["loc"], flt["days"]))
        t = ui.table(columns=INC_COLUMNS, rows=[incident_row(r) for r in rows], row_key="id", pagination=15) \
            .classes("w-full")
        badge_slot(t, "status")
        badge_slot(t, "severity")
        t.on("rowClick", lambda e: ui.navigate.to(f"/incidents/{e.args[1]['id']}"))
        ui.label(f"Знайдено: {len(rows)}").classes("text-sm text-grey-7")

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("items-end gap-4"):
            ui.select({"open": "Відкриті (не прийняті й у роботі)", "all": "Усі",
                       **{k: v[0] for k, v in INC_STATUS.items()}}, label="Статус", value=flt["status"],
                      on_change=lambda e: (flt.update(status=e.value), table.refresh())).classes("w-72")
            ui.select({None: "усі", **locs}, label="Вузол", value=None,
                      on_change=lambda e: (flt.update(loc=e.value), table.refresh())).classes("w-52")
            ui.select({1: "доба", 7: "7 діб", 30: "30 діб", 365: "рік"}, label="Період",
                      value=days if days in (1, 7, 30, 365) else 7,
                      on_change=lambda e: (flt.update(days=e.value), table.refresh())).classes("w-32")
        await table()
    ui.timer(20, table.refresh)


@ui.page("/incidents/{inc_id}")
async def incident_card(inc_id: int):
    i = await db.one("SELECT * FROM v_incident_list WHERE id = %s", (inc_id,))
    if not i:
        await frame("Інцидент не знайдено")
        return
    await frame(f"Інцидент №{i['id']}", f"{i['equipment']} · {i['location']}")
    r = incident_row(i)

    async def act(sql: str, params: tuple, ok: str):
        try:
            await db.run(sql, params, user_id=uid())
        except Exception as e:
            ui.notify(error_text(e), type="negative", multi_line=True)
            return
        ui.notify(ok, type="positive")
        ui.navigate.to(f"/incidents/{inc_id}")

    def close_dialog():
        with ui.dialog() as dlg, ui.card().classes("w-[520px] p-6 gap-3"):
            ui.label("Закриття інциденту").classes("text-lg font-semibold")
            comment = ui.textarea("Коментар щодо вжитих заходів (обов'язково)").props("outlined autogrow").classes("w-full")

            async def do_close():
                if not (comment.value or "").strip():
                    ui.notify("Коментар обов'язковий", type="warning")
                    return
                dlg.close()
                await act("CALL sp_close_incident(%s, %s, %s)", (inc_id, uid(), comment.value), "Інцидент закрито")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Скасувати", on_click=dlg.close).props("flat")
                ui.button("Закрити інцидент", icon="task_alt", on_click=do_close).props("color=positive")
        dlg.open()

    with ui.row().classes("w-full gap-4 items-stretch"):
        with ui.card().classes("flex-[2] min-w-[420px] p-5"):
            with ui.row().classes("items-center gap-2"):
                badge(r["status_label"], r["status_color"])
                badge(r["severity_label"], r["severity_color"])
                ui.label("Порушення порогу" if i["incident_type"] == "threshold" else "Недоступність пристрою") \
                    .classes("text-grey-8")
            ui.label(r["what"]).classes("text-xl font-semibold mt-2")
            info = [("Пристрій", i["equipment"]), ("Вузол", i["location"]), ("Норма", r["norm"]),
                    ("Відкрито", fmt_dt(i["opened_at"], True)), ("Прийняв", r["ack"]),
                    ("Коментар закриття", i["close_comment"] or "—")]
            with ui.grid(columns=2).classes("gap-x-6 gap-y-1 mt-2"):
                for k, v in info:
                    ui.label(k).classes("text-sm text-grey-7")
                    ui.label(v).classes("text-sm")
            with ui.row().classes("gap-2 mt-4"):
                if i["status"] in ("new", "escalated"):
                    ui.button("Прийняти в роботу", icon="pan_tool", on_click=lambda: act(
                        "CALL sp_acknowledge_incident(%s, %s)", (inc_id, uid()), "Інцидент прийнято"))
                if i["status"] == "acknowledged":
                    ui.button("Позначити усунутим", icon="build_circle", color="info", on_click=lambda: act(
                        "CALL sp_resolve_incident(%s, %s)", (inc_id, uid()), "Несправність позначено усунутою"))
                if i["status"] == "resolved":
                    ui.button("Закрити з коментарем", icon="task_alt", color="positive", on_click=close_dialog)
                ui.button("Картка пристрою", icon="router",
                          on_click=lambda: ui.navigate.to(f"/equipment/{i['equipment_id']}")).props("outline")
        with ui.card().classes("flex-1 min-w-[300px] p-5"):
            ui.label("Хронологія").classes("text-lg font-semibold")
            with ui.timeline(side="right").classes("mt-1"):
                steps = [("Виявлено", i["opened_at"], "error"), ("Ескалація адміністратору", i["escalated_at"], "campaign"),
                         ("Прийнято в роботу", i["acknowledged_at"], "pan_tool"),
                         ("Несправність усунуто", i["resolved_at"], "build_circle"),
                         ("Закрито", i["closed_at"], "task_alt")]
                for title, at, icon in steps:
                    if at:
                        ui.timeline_entry(subtitle=fmt_dt(at, True), title=title, icon=icon)

    if i["incident_type"] == "threshold":
        hist = await db.all("SELECT * FROM fn_metric_history(%s, %s, %s, %s, '5 minutes')",
                            (i["equipment_id"], i["metric_code"], i["opened_at"] - timedelta(hours=3),
                             (i["closed_at"] or i["resolved_at"] or datetime.now().astimezone()) + timedelta(hours=1)))
        marks = [{"yAxis": float(v), "lineStyle": {"color": "#d62828", "type": "dashed"}}
                 for v in (i["min_value"], i["max_value"]) if v is not None]
        with ui.card().classes("w-full p-4"):
            ui.label(f"{i['metric_name']} навколо моменту аварії, {i['unit'] or ''}").classes("font-semibold")
            ui.echart({"grid": {"left": 50, "right": 30, "top": 20, "bottom": 30}, "tooltip": {"trigger": "axis"},
                       "xAxis": {"type": "time"}, "yAxis": {"type": "value", "scale": True},
                       "series": [{"type": "line", "showSymbol": False, "lineStyle": {"color": "#1d3557"},
                                   "data": [[h["bucket"].isoformat(), round(h["avg_value"], 2)] for h in hist],
                                   "markLine": {"symbol": "none", "data": marks},
                                   "markPoint": {"data": [{"coord": [i["opened_at"].isoformat(), float(i["metric_value"])],
                                                           "value": "аварія"}]}}]}).classes("w-full h-60")

    notes = await db.all(
        "SELECT n.*, u.full_name FROM notification n JOIN users u ON u.id = n.user_id WHERE n.incident_id = %s"
        " ORDER BY n.created_at, u.full_name, n.channel", (inc_id,))
    ui.label("Сповіщення за інцидентом").classes("text-lg font-semibold")
    t = ui.table(columns=[{"name": "created", "label": "Поставлено в чергу", "field": "created", "align": "left"},
                          {"name": "user", "label": "Отримувач", "field": "user", "align": "left"},
                          {"name": "channel", "label": "Канал", "field": "channel", "align": "left"},
                          {"name": "kind", "label": "Вид", "field": "kind", "align": "left"},
                          {"name": "status", "label": "Стан", "field": "status_label", "align": "left"},
                          {"name": "sent", "label": "Надіслано", "field": "sent", "align": "left"},
                          {"name": "error", "label": "Помилка", "field": "error", "align": "left"}],
                 rows=[{"id": n["id"], "created": fmt_dt(n["created_at"], True), "user": n["full_name"],
                        "channel": CHANNEL[n["channel"]], "kind": "ескалація" if n["is_escalation"] else "звичайне",
                        "status_label": NOTIFY_STATUS[n["status"]][0], "status_color": NOTIFY_STATUS[n["status"]][1],
                        "sent": fmt_dt(n["sent_at"], True), "error": n["error_message"] or ""} for n in notes],
                 row_key="id", pagination=10).classes("w-full")
    badge_slot(t, "status")


# ---------------------------------------------------------------------------- сповіщення користувача
@ui.page("/notifications")
async def my_notifications():
    await frame("Мої сповіщення", "Сповіщення про інциденти, надіслані вам усіма каналами")
    rows = await db.all(
        "SELECT n.*, i.equipment, i.location, i.metric_name, i.metric_value, i.unit, i.incident_type, i.severity,"
        " i.status AS inc_status FROM notification n JOIN v_incident_list i ON i.id = n.incident_id"
        " WHERE n.user_id = %s ORDER BY n.created_at DESC LIMIT 300", (uid(),))
    data = []
    for n in rows:
        sl, sc = NOTIFY_STATUS[n["status"]]
        vl, vc = SEVERITY[n["severity"]]
        data.append({"id": n["id"], "incident": n["incident_id"], "created": fmt_dt(n["created_at"], True),
                     "equipment": f"{n['equipment']} ({n['location']})",
                     "what": "немає відповіді" if n["incident_type"] == "offline" else
                     f"{n['metric_name']} = {fmt_num(n['metric_value'])} {n['unit'] or ''}",
                     "severity_label": vl, "severity_color": vc,
                     "channel": CHANNEL[n["channel"]] + (" · ескалація" if n["is_escalation"] else ""),
                     "status_label": sl, "status_color": sc, "inc": INC_STATUS[n["inc_status"]][0]})
    t = ui.table(columns=[{"name": "created", "label": "Час", "field": "created", "align": "left"},
                          {"name": "incident", "label": "Інцидент", "field": "incident"},
                          {"name": "equipment", "label": "Пристрій", "field": "equipment", "align": "left"},
                          {"name": "what", "label": "Аварія", "field": "what", "align": "left"},
                          {"name": "severity", "label": "Рівень", "field": "severity_label", "align": "left"},
                          {"name": "channel", "label": "Канал", "field": "channel", "align": "left"},
                          {"name": "status", "label": "Доставка", "field": "status_label", "align": "left"},
                          {"name": "inc", "label": "Статус інциденту", "field": "inc", "align": "left"}],
                 rows=data, row_key="id", pagination=15).classes("w-full")
    badge_slot(t, "status")
    badge_slot(t, "severity")
    t.on("rowClick", lambda e: ui.navigate.to(f"/incidents/{e.args[1]['incident']}"))


# ---------------------------------------------------------------------------- журнал подій
@ui.page("/events")
async def events_page():
    await frame("Журнал подій", "Зміни стану обладнання, інциденти, дії користувачів, зміни конфігурації, автентифікація")
    locs = {r["id"]: r["name"] for r in await db.all("SELECT id, name FROM locations ORDER BY name")}
    today = date.today()
    flt = {"from": str(today - timedelta(days=7)), "to": str(today), "type": None, "loc": None, "q": ""}
    state = {"rows": []}

    @ui.refreshable
    async def table():
        rows = await db.all(
            "SELECT ev.*, u.full_name, e.name AS equipment, l.name AS location FROM event_log ev"
            " LEFT JOIN users u ON u.id = ev.user_id LEFT JOIN equipment e ON e.id = ev.equipment_id"
            " LEFT JOIN locations l ON l.id = e.location_id"
            " WHERE ev.created_at >= %(f)s::date AND ev.created_at < %(t)s::date + 1"
            "   AND (%(type)s::text IS NULL OR ev.event_type::text = %(type)s)"
            "   AND (%(loc)s::int IS NULL OR e.location_id = %(loc)s) AND ev.message ILIKE %(q)s"
            " ORDER BY ev.created_at DESC, ev.id DESC LIMIT 1000",
            {"f": flt["from"], "t": flt["to"], "type": flt["type"], "loc": flt["loc"], "q": f"%{flt['q']}%"})
        state["rows"] = rows
        data = [{"id": r["id"], "at": fmt_dt(r["created_at"], True), "type_label": EVENT[r["event_type"]][0],
                 "type_color": EVENT[r["event_type"]][1], "user": r["full_name"] or "система",
                 "equipment": r["equipment"] or "—", "location": r["location"] or "—", "message": r["message"],
                 "details": _short(_details(r["details"]))} for r in rows]
        t = ui.table(columns=[{"name": "at", "label": "Час", "field": "at", "align": "left"},
                              {"name": "type", "label": "Тип", "field": "type_label", "align": "left"},
                              {"name": "user", "label": "Користувач", "field": "user", "align": "left"},
                              {"name": "equipment", "label": "Пристрій", "field": "equipment", "align": "left"},
                              {"name": "location", "label": "Вузол", "field": "location", "align": "left"},
                              {"name": "message", "label": "Подія", "field": "message", "align": "left"},
                              {"name": "details", "label": "Деталі", "field": "details", "align": "left",
                               "style": "max-width: 340px; white-space: normal"}],
                     rows=data, row_key="id", pagination=20).classes("w-full")
        badge_slot(t, "type")
        ui.label(f"Подій: {len(rows)}" + (" (показано перші 1000)" if len(rows) == 1000 else "")) \
            .classes("text-sm text-grey-7")

    def export_csv():
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Час", "Тип", "Користувач", "Пристрій", "Вузол", "Подія", "Деталі"])
        for r in state["rows"]:
            w.writerow([fmt_dt(r["created_at"], True), EVENT[r["event_type"]][0], r["full_name"] or "система",
                        r["equipment"] or "", r["location"] or "", r["message"], _details(r["details"])])
        ui.download.content(("﻿" + buf.getvalue()).encode("utf-8"), "netguard_events.csv", "text/csv")

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("items-end gap-4"):
            ui.input("Від", value=flt["from"], on_change=lambda e: (flt.update({"from": e.value}), table.refresh())) \
                .props("type=date").classes("w-40")
            ui.input("До", value=flt["to"], on_change=lambda e: (flt.update(to=e.value), table.refresh())) \
                .props("type=date").classes("w-40")
            ui.select({None: "усі", **{k: v[0] for k, v in EVENT.items()}}, label="Тип події", value=None,
                      on_change=lambda e: (flt.update(type=e.value), table.refresh())).classes("w-48")
            ui.select({None: "усі", **locs}, label="Вузол", value=None,
                      on_change=lambda e: (flt.update(loc=e.value), table.refresh())).classes("w-48")
            ui.input("Пошук у тексті", on_change=lambda e: (flt.update(q=e.value or ""), table.refresh())) \
                .props("clearable").classes("w-48")
            ui.space()
            ui.button("CSV", icon="download", on_click=export_csv).props("outline")
        await table()


def _short(text: str, limit: int = 220) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _details(d) -> str:
    if not d:
        return ""
    if isinstance(d, str):
        d = json.loads(d)
    return "; ".join(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}"
                     for k, v in d.items() if v is not None)

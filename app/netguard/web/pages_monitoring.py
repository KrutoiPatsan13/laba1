"""Панель моніторингу (FR-05), облік обладнання (FR-02, FR-03) і графіки показників (FR-12).

Дані й зміни — через API Gateway (/api/dashboard, /api/reference, /api/equipment).
"""
from datetime import date

from nicegui import ui

from . import api
from .common import (CATEGORY, EQ_STATUS, INC_STATUS, SEVERITY, badge, badge_slot, dt, fmt_dt, fmt_num, frame,
                     is_admin, stat_card)

UNITS = {"battery_charge": "%", "battery_temperature": "°C", "battery_runtime": "хв", "battery_voltage": "В",
         "input_voltage": "В", "output_voltage": "В", "output_load": "%", "cpu_load": "%", "temperature": "°C",
         "uptime": "д"}
SHORT = {"battery_charge": "АКБ", "input_voltage": "Вхід", "output_load": "Навант.", "battery_temperature": "t АКБ",
         "battery_runtime": "Автономно", "cpu_load": "CPU", "temperature": "t", "uptime": "Uptime"}
KEY_METRICS = {"ups": ["battery_charge", "input_voltage", "output_load", "battery_runtime", "battery_temperature"],
               "switch": ["cpu_load", "temperature", "uptime"], "router": ["cpu_load", "temperature", "uptime"],
               "onu": ["temperature", "uptime"]}


def metric_str(code: str, v) -> str:
    if v is None:
        return "—"
    if code == "battery_runtime":
        v = float(v) / 60
    elif code == "uptime":
        v = float(v) / 86400
    return f"{SHORT[code]} {fmt_num(v, 1)} {UNITS[code]}"


def device_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        lv = r["last_values"] or {}
        label, color = EQ_STATUS[r["status"]]
        out.append({
            "id": r["id"], "name": r["name"], "ip": r["ip"], "type": CATEGORY.get(r["category"], r["category"]),
            "status_label": label if r["is_active"] else "Виведено", "status_color": color if r["is_active"] else "grey-5",
            "metrics": " · ".join(metric_str(c, lv.get(c)) for c in KEY_METRICS.get(r["category"], []) if c in lv) or "—",
            "last_seen": dt(r["last_seen_at"]).strftime("%H:%M:%S") if r["last_seen_at"] else "—",
            "incidents": r["open_incidents"] or "",
            "location": r["location"],
        })
    return out


DEV_COLUMNS = [
    {"name": "name", "label": "Пристрій", "field": "name", "align": "left", "sortable": True},
    {"name": "type", "label": "Тип", "field": "type", "align": "left"},
    {"name": "ip", "label": "IP-адреса", "field": "ip", "align": "left"},
    {"name": "status", "label": "Стан", "field": "status_label", "align": "left"},
    {"name": "metrics", "label": "Останні показники", "field": "metrics", "align": "left",
     "style": "white-space: normal; min-width: 240px"},
    {"name": "last_seen", "label": "Відповідь", "field": "last_seen", "align": "left"},
    {"name": "incidents", "label": "Відкр. інцид.", "field": "incidents", "align": "center"},
]


@ui.page("/")
async def dashboard():
    await frame("Панель моніторингу", "Поточний стан обладнання та ДБЖ усіх вузлів провайдера; оновлюється кожні 15 с")

    @ui.refreshable
    async def body():
        try:
            data = await api.get("/api/dashboard")
        except api.ApiError as e:
            ui.label(f"Дані недоступні: {e.message}").classes("text-negative")
            return
        s, rows, incidents = data["summary"], data["devices"], data["incidents"]
        with ui.row().classes("w-full gap-4"):
            stat_card("check_circle", "пристроїв на зв'язку", f"{s['online']} / {s['total']}", "positive")
            stat_card("cloud_off", "пристроїв офлайн", str(s["offline"]), "negative" if s["offline"] else "grey-7")
            stat_card("warning", "відкритих інцидентів", str(s["open_inc"]), "warning" if s["open_inc"] else "grey-7",
                      f"критичних: {s['critical']}, ескальованих: {s['escalated']}")
            stat_card("battery_charging_full", "мінімальний заряд АКБ ДБЖ",
                      f"{fmt_num(s['min_charge'])} %" if s["min_charge"] is not None else "—",
                      "negative" if s["min_charge"] is not None and s["min_charge"] < 50 else "positive")

        with ui.row().classes("w-full gap-4 items-start no-wrap max-lg:flex-wrap"):
            with ui.column().classes("flex-[3] gap-4 min-w-0"):
                by_loc = {}
                for r in device_rows(rows):
                    by_loc.setdefault(r["location"], []).append(r)
                for loc, items in by_loc.items():
                    bad = sum(1 for i in items if i["status_label"] == "Офлайн")
                    with ui.card().classes("w-full p-0"):
                        with ui.row().classes("items-center justify-between w-full px-4 pt-3"):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("location_on").classes("text-primary text-xl")
                                ui.label(loc).classes("text-lg font-semibold")
                            ui.label(f"{len(items)} пристроїв" + (f", офлайн: {bad}" if bad else "")) \
                                .classes("text-sm " + ("text-negative" if bad else "text-grey-7"))
                        t = ui.table(columns=DEV_COLUMNS, rows=items, row_key="id").classes("w-full").props(
                            "flat dense hide-pagination :pagination='{rowsPerPage: 0}'")
                        badge_slot(t, "status")
                        t.on("rowClick", lambda e: ui.navigate.to(f"/equipment/{e.args[1]['id']}"))
            with ui.card().classes("flex-1 min-w-[320px] p-4"):
                ui.label("Відкриті інциденти").classes("text-lg font-semibold")
                if not incidents:
                    ui.label("Аварій немає").classes("text-grey-7")
                for i in incidents:
                    sev_label, sev_color = SEVERITY[i["severity"]]
                    st_label, st_color = INC_STATUS[i["status"]]
                    with ui.card().classes("w-full p-3 cursor-pointer hover:bg-grey-2").on(
                            "click", lambda i=i: ui.navigate.to(f"/incidents/{i['id']}")):
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label(f"№{i['id']} · {i['equipment']}").classes("font-semibold")
                            badge(st_label, st_color)
                        what = "немає відповіді на SNMP" if i["incident_type"] == "offline" else \
                            f"{i['metric_name']}: {fmt_num(i['metric_value'])} {i['unit'] or ''}"
                        ui.label(what).classes("text-sm")
                        with ui.row().classes("items-center justify-between w-full"):
                            badge(sev_label, sev_color)
                            ui.label(f"{i['location']} · {fmt_dt(i['opened_at'])}").classes("text-xs text-grey-7")

    await body()
    ui.timer(15, body.refresh)


# ---------------------------------------------------------------------------- обладнання
@ui.page("/equipment")
async def equipment_list():
    await frame("Обладнання", "Мережеве обладнання та ДБЖ; клацніть рядок, щоб відкрити картку пристрою з графіками")
    try:
        refs = await reference_lists()
    except api.ApiError as e:
        ui.label(f"Дані недоступні: {e.message}").classes("text-negative")
        return
    flt = {"location": None, "category": None, "status": None, "q": ""}

    @ui.refreshable
    async def table():
        try:
            rows = await api.get("/api/equipment", {"location_id": flt["location"], "category": flt["category"],
                                                    "status": flt["status"], "q": flt["q"]})
        except api.ApiError as e:
            ui.label(f"Дані недоступні: {e.message}").classes("text-negative")
            return
        data = device_rows(rows)
        for d, r in zip(data, rows):
            d.update(location=r["location"], interval=f"{r['poll_interval_s']} с", model=r["model"] or "—",
                     source="YAML" if r["config_id"] else "вручну")
        cols = DEV_COLUMNS[:3] + [{"name": "location", "label": "Вузол", "field": "location", "align": "left",
                                   "sortable": True},
                                  {"name": "model", "label": "Модель", "field": "model", "align": "left"}] + \
            DEV_COLUMNS[3:4] + [{"name": "interval", "label": "Інтервал", "field": "interval"},
                                {"name": "source", "label": "Джерело", "field": "source"}] + DEV_COLUMNS[5:]
        t = ui.table(columns=cols, rows=data, row_key="id", pagination=15).classes("w-full")
        badge_slot(t, "status")
        t.on("rowClick", lambda e: ui.navigate.to(f"/equipment/{e.args[1]['id']}"))
        ui.label(f"Знайдено: {len(data)}").classes("text-sm text-grey-7")

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("items-end gap-4 w-full"):
            ui.select({None: "усі", **refs["locations"]}, label="Вузол", value=None,
                      on_change=lambda e: (flt.update(location=e.value), table.refresh())).classes("w-48")
            ui.select({None: "усі", **CATEGORY}, label="Категорія", value=None,
                      on_change=lambda e: (flt.update(category=e.value), table.refresh())).classes("w-40")
            ui.select({None: "усі", **{k: v[0] for k, v in EQ_STATUS.items()}, "retired": "Виведено"}, label="Стан",
                      value=None, on_change=lambda e: (flt.update(status=e.value), table.refresh())).classes("w-40")
            ui.input("Пошук за назвою", on_change=lambda e: (flt.update(q=e.value or ""), table.refresh())) \
                .props("clearable").classes("w-56")
            ui.space()
            if is_admin():
                ui.button("Додати обладнання", icon="add", on_click=lambda: equipment_dialog(refs, table.refresh))
                ui.button("Зареєструвати ДБЖ", icon="battery_charging_full", color="secondary",
                          on_click=lambda: ups_dialog(refs, table.refresh))
        await table()


async def reference_lists() -> dict:
    ref = await api.get("/api/reference")
    return {"locations": {r["id"]: r["name"] for r in ref["locations"]},
            "types": {r["id"]: f"{r['name']} ({CATEGORY[r['category']]})" for r in ref["types"]},
            "ups_types": {r["id"]: r["name"] for r in ref["types"] if r["category"] == "ups"},
            "credentials": {r["id"]: f"{r['name']} (SNMP {r['snmp_version']})" for r in ref["credentials"]}}


def equipment_dialog(refs: dict, on_saved, eq: dict | None = None):
    """Додавання або редагування обладнання (адміністратор)."""
    with ui.dialog() as dlg, ui.card().classes("w-[560px] p-6 gap-3"):
        ui.label("Редагування обладнання" if eq else "Нове обладнання").classes("text-lg font-semibold")
        eq = eq or {}
        with ui.grid(columns=2).classes("w-full gap-3"):
            name = ui.input("Мережеве ім'я (hostname)", value=eq.get("name", "")).props("outlined")
            ip = ui.input("IP-адреса", value=eq.get("ip", "")).props("outlined")
            type_id = ui.select(refs["types"], label="Тип обладнання", value=eq.get("type_id")).props("outlined")
            loc = ui.select(refs["locations"], label="Вузол", value=eq.get("location_id")).props("outlined")
            cred = ui.select(refs["credentials"], label="Обліковий запис SNMP", value=eq.get("credential_id")) \
                .props("outlined")
            port = ui.number("UDP-порт SNMP", value=eq.get("snmp_port", 161), min=1, max=65535, format="%d") \
                .props("outlined")
            interval = ui.number("Інтервал опитування, с", value=eq.get("poll_interval_s", 60), min=10, max=60,
                                 format="%d").props("outlined")
            model = ui.input("Модель", value=eq.get("model") or "").props("outlined")
            serial = ui.input("Серійний номер", value=eq.get("serial_number") or "").props("outlined")
            installed = ui.input("Дата встановлення", value=str(eq.get("installed_on") or "")).props("outlined type=date")

        async def save():
            body = {"name": name.value.strip(), "ip": ip.value.strip(), "type_id": type_id.value, "location_id": loc.value,
                    "credential_id": cred.value, "snmp_port": int(port.value or 161),
                    "poll_interval_s": int(interval.value or 60), "model": model.value.strip() or None,
                    "serial_number": serial.value.strip() or None, "installed_on": installed.value or None}
            try:
                if eq.get("id"):
                    await api.put(f"/api/equipment/{eq['id']}", body)
                else:
                    await api.post("/api/equipment", body)
            except api.ApiError as e:
                ui.notify(f"Не збережено: {e.message}", type="negative", multi_line=True)
                return
            ui.notify("Збережено", type="positive")
            dlg.close()
            await on_saved()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Скасувати", on_click=dlg.close).props("flat")
            ui.button("Зберегти", icon="save", on_click=save)
    dlg.open()


def ups_dialog(refs: dict, on_saved):
    """Реєстрація ДБЖ однією транзакцією: обладнання + паспорт (функція fn_register_ups)."""
    with ui.dialog() as dlg, ui.card().classes("w-[600px] p-6 gap-3"):
        ui.label("Реєстрація ДБЖ").classes("text-lg font-semibold")
        with ui.grid(columns=2).classes("w-full gap-3"):
            name = ui.input("Мережеве ім'я").props("outlined")
            ip = ui.input("IP-адреса картки NMC").props("outlined")
            type_id = ui.select(refs["ups_types"], label="Модель ДБЖ (тип)").props("outlined")
            loc = ui.select(refs["locations"], label="Вузол").props("outlined")
            cred = ui.select(refs["credentials"], label="Обліковий запис SNMP").props("outlined")
            model = ui.input("Модель за паспортом").props("outlined")
            va = ui.number("Повна потужність, В·А", min=1, format="%d").props("outlined")
            w = ui.number("Активна потужність, Вт", min=1, format="%d").props("outlined")
            ah = ui.number("Ємність АКБ, А·год", min=0.1, step=0.1).props("outlined")
            cnt = ui.number("Кількість батарейних модулів", value=1, min=1, format="%d").props("outlined")
            volt = ui.number("Номінальна напруга АКБ, В", min=1).props("outlined")
            bdate = ui.input("Дата встановлення АКБ").props("outlined type=date")

        async def save():
            try:
                await api.post("/api/equipment/ups", {
                    "type_id": type_id.value, "location_id": loc.value, "name": name.value.strip(), "ip": ip.value.strip(),
                    "credential_id": cred.value, "rated_power_va": int(va.value or 0), "battery_capacity_ah": ah.value,
                    "rated_power_w": int(w.value) if w.value else None, "battery_count": int(cnt.value or 1),
                    "nominal_voltage_v": volt.value, "model": model.value.strip() or None,
                    "battery_installed_on": bdate.value or None})
            except api.ApiError as e:
                ui.notify(f"ДБЖ не зареєстровано: {e.message}", type="negative", multi_line=True)
                return
            ui.notify("ДБЖ зареєстровано", type="positive")
            dlg.close()
            await on_saved()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Скасувати", on_click=dlg.close).props("flat")
            ui.button("Зареєструвати", icon="save", on_click=save)
    dlg.open()


# ---------------------------------------------------------------------------- картка пристрою
PERIODS = {"6h": "6 годин", "24h": "24 години", "7d": "7 діб"}   # тривалість і крок агрегування задає API Gateway


@ui.page("/equipment/{eq_id}")
async def equipment_card(eq_id: int, period: str = "24h"):
    try:
        eq = await api.get(f"/api/equipment/{eq_id}")
    except api.ApiError as e:
        await frame("Обладнання не знайдено" if e.status == 404 else e.message)
        return
    await frame(eq["name"], f"{eq['type_name']} · {eq['location']} · {eq['ip']}")
    refs = await reference_lists() if is_admin() else None

    with ui.row().classes("w-full gap-4 items-stretch"):
        with ui.card().classes("flex-1 min-w-[360px] p-4"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Відомості").classes("text-lg font-semibold")
                label, color = EQ_STATUS[eq["status"]] if eq["is_active"] else ("Виведено з експлуатації", "grey-5")
                badge(label, color)
            fields = [("Категорія", CATEGORY[eq["category"]]), ("Модель", eq["model"] or "—"),
                      ("Серійний номер", eq["serial_number"] or "—"), ("SNMP", f"{eq['credential']} ({eq['snmp_version']}),"
                                                                        f" порт {eq['snmp_port']}"),
                      ("Інтервал опитування", f"{eq['poll_interval_s']} с"),
                      ("Профіль YAML", eq["snmp_profile"] or "—"),
                      ("Джерело запису", f"YAML, версія №{eq['config_id']}" if eq["config_id"] else "додано вручну"),
                      ("Невдалих опитувань поспіль", str(eq["failed_polls"])),
                      ("Остання відповідь", fmt_dt(eq["last_seen_at"], seconds=True)),
                      ("Встановлено", fmt_dt(eq["installed_on"]))]
            with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-1 mt-2"):
                for k, v in fields:
                    ui.label(k).classes("text-sm text-grey-7")
                    ui.label(v).classes("text-sm")
            if is_admin():
                with ui.row().classes("gap-2 mt-3"):
                    ui.button("Редагувати", icon="edit", on_click=lambda: equipment_dialog(
                        refs, lambda: _reload(eq_id), dict(eq))).props("outline")
                    if eq["is_active"]:
                        maint = eq["status"] == "maintenance"
                        ui.button("Завершити обслуговування" if maint else "Обслуговування", icon="build",
                                  on_click=lambda: _set(eq_id, "end_maintenance" if maint else "maintenance")
                                  ).props("outline color=warning")
                        ui.button("Вивести з експлуатації", icon="block",
                                  on_click=lambda: _set(eq_id, "retire")).props("outline color=negative")
                    else:
                        ui.button("Повернути в експлуатацію", icon="restore",
                                  on_click=lambda: _set(eq_id, "restore")).props("outline color=positive")
        if eq["category"] == "ups":
            with ui.card().classes("w-[340px] p-4"):
                ui.label("Паспорт ДБЖ").classes("text-lg font-semibold")
                if eq["rated_power_va"]:
                    age = (date.today() - dt(eq["battery_installed_on"])).days if eq["battery_installed_on"] else None
                    ups = [("Повна потужність", f"{eq['rated_power_va']} В·А"),
                           ("Активна потужність", f"{eq['rated_power_w'] or '—'} Вт"),
                           ("Ємність АКБ", f"{fmt_num(eq['battery_capacity_ah'])} А·год"),
                           ("Батарейних модулів", str(eq["battery_count"])),
                           ("Номінальна напруга АКБ", f"{fmt_num(eq['nominal_voltage_v'])} В"),
                           ("АКБ встановлено", fmt_dt(eq["battery_installed_on"])),
                           ("Вік АКБ", f"{age // 365} р. {age % 365 // 30} міс." if age is not None else "—")]
                    with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1 mt-2"):
                        for k, v in ups:
                            ui.label(k).classes("text-sm text-grey-7")
                            ui.label(v).classes("text-sm")
                else:
                    ui.label("Паспорт не заповнено").classes("text-grey-7")

    state = {"period": period if period in PERIODS else "24h"}

    @ui.refreshable
    async def charts():
        try:
            data = await api.get(f"/api/equipment/{eq_id}/history", {"period": state["period"]})
        except api.ApiError as e:
            ui.label(f"Історія недоступна: {e.message}").classes("text-negative")
            return
        with ui.grid(columns=2).classes("w-full gap-4 max-lg:grid-cols-1"):
            for m in data["metrics"]:
                k = 1 / 60 if m["code"] == "battery_runtime" else 1 / 86400 if m["code"] == "uptime" else 1
                unit = "хв" if m["code"] == "battery_runtime" else "д" if m["code"] == "uptime" else m["unit"]
                lines = []
                for t in data["thresholds"]:
                    if t["code"] == m["code"]:
                        for v in (t["min_value"], t["max_value"]):
                            if v is not None:
                                sev = "критично" if t["severity"] == "critical" else "попередження"
                                lines.append({"yAxis": float(v) * k, "name": sev,
                                              "lineStyle": {"color": "#d62828" if t["severity"] == "critical" else "#f4a261",
                                                            "type": "dashed"}, "label": {"formatter": sev}})
                with ui.card().classes("w-full p-3"):
                    ui.label(f"{m['name']}, {unit}").classes("font-semibold")
                    ui.echart({
                        "grid": {"left": 50, "right": 95, "top": 20, "bottom": 30},
                        "tooltip": {"trigger": "axis"},
                        "xAxis": {"type": "time"},
                        "yAxis": {"type": "value", "scale": True},
                        "series": [{"type": "line", "name": m["name"], "showSymbol": False, "smooth": True,
                                    "lineStyle": {"width": 2, "color": "#1d3557"}, "areaStyle": {"opacity": 0.06},
                                    "data": [[h["bucket"], round(h["avg_value"] * k, 2)] for h in m["history"]],
                                    "markLine": {"symbol": "none", "data": lines}}],
                    }).classes("w-full h-56")

    with ui.row().classes("items-center gap-3 w-full mt-2"):
        ui.label("Історія показників").classes("text-lg font-semibold")
        ui.toggle(PERIODS, value=state["period"],
                  on_change=lambda e: (state.update(period=e.value), charts.refresh()))
    await charts()

    inc = await api.get(f"/api/equipment/{eq_id}/incidents")
    ui.label("Інциденти пристрою").classes("text-lg font-semibold mt-2")
    rows = []
    for i in inc:
        sl, sc = INC_STATUS[i["status"]]
        rows.append({"id": i["id"], "opened": fmt_dt(i["opened_at"]), "status_label": sl, "status_color": sc,
                     "what": "немає відповіді" if i["incident_type"] == "offline" else
                     f"{i['metric_name']} = {fmt_num(i['metric_value'])} {i['unit'] or ''}",
                     "severity": SEVERITY[i["severity"]][0], "ack": i["acknowledged_by_name"] or "—"})
    t = ui.table(columns=[{"name": "id", "label": "№", "field": "id"},
                          {"name": "opened", "label": "Відкрито", "field": "opened", "align": "left"},
                          {"name": "what", "label": "Аварія", "field": "what", "align": "left"},
                          {"name": "severity", "label": "Рівень", "field": "severity", "align": "left"},
                          {"name": "status", "label": "Статус", "field": "status_label", "align": "left"},
                          {"name": "ack", "label": "Прийняв", "field": "ack", "align": "left"}],
                 rows=rows, row_key="id", pagination=10).classes("w-full")
    badge_slot(t, "status")
    t.on("rowClick", lambda e: ui.navigate.to(f"/incidents/{e.args[1]['id']}"))


async def _set(eq_id: int, action: str):
    try:
        await api.patch(f"/api/equipment/{eq_id}/state", {"action": action})
    except api.ApiError as e:
        ui.notify(f"Не виконано: {e.message}", type="negative", multi_line=True)
        return
    await _reload(eq_id)


async def _reload(eq_id: int):
    ui.navigate.to(f"/equipment/{eq_id}")

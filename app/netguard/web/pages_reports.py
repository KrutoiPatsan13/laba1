"""Звіти за період (FR-10) з експортом у Excel, PDF і CSV."""
from datetime import date, datetime, time, timedelta

from nicegui import ui

from .. import reports as rp
from .common import db, frame


@ui.page("/reports")
async def reports_page():
    await frame("Звіти", "Доступність обладнання, статистика інцидентів (MTTA, MTTR) і стан АКБ ДБЖ за обраний період")
    today = date.today()
    flt = {"from": str(today - timedelta(days=7)), "to": str(today)}
    state: dict = {"reports": [], "period": ""}

    def period():
        f = datetime.combine(date.fromisoformat(flt["from"]), time.min).astimezone()
        t = min(datetime.combine(date.fromisoformat(flt["to"]), time.min).astimezone() + timedelta(days=1),
                datetime.now().astimezone())
        return f, t

    @ui.refreshable
    async def body():
        f, t = period()
        if f >= t:
            ui.label("Початок періоду має бути раніше за кінець").classes("text-negative")
            return
        state["reports"] = await rp.build(db, f, t)
        state["period"] = rp.period_title(f, t)
        up, inc, ups = state["reports"]
        with ui.row().classes("w-full gap-4 items-stretch"):
            avg = sum(r[4] for r in up.rows) / len(up.rows) if up.rows else 0
            worst = min(up.rows, key=lambda r: r[4]) if up.rows else None
            total = sum(r[2] for r in inc.rows)
            for title, value, hint in [("Середня доступність", f"{avg:.2f} %", f"найнижча: {worst[0]} — {worst[4]} %" if worst else ""),
                                       ("Інцидентів за період", str(total),
                                        f"критичних: {sum(r[3] for r in inc.rows)}, недоступність: {sum(r[4] for r in inc.rows)}"),
                                       ("ДБЖ під контролем", str(len(ups.rows)),
                                        f"мін. заряд за період: {min((r[6] for r in ups.rows if r[6] is not None), default='—')} %")]:
                with ui.card().classes("flex-1 p-4"):
                    ui.label(title).classes("text-sm text-grey-7")
                    ui.label(value).classes("text-2xl font-bold text-primary")
                    ui.label(hint).classes("text-xs text-grey-7")
        with ui.tabs().classes("w-full") as tabs:
            tab_objs = [ui.tab(r.key, label=r.title) for r in state["reports"]]
        with ui.tab_panels(tabs, value=tab_objs[0]).classes("w-full"):
            for rep, tab in zip(state["reports"], tab_objs):
                with ui.tab_panel(tab):
                    if rep.key == "incidents" and rep.rows:
                        ui.echart({"grid": {"left": 120, "right": 30, "top": 30, "bottom": 30},
                                   "legend": {"top": 0}, "tooltip": {"trigger": "axis"},
                                   "xAxis": {"type": "value"},
                                   "yAxis": {"type": "category", "data": [f"{r[0]} / {r[1]}" for r in rep.rows]},
                                   "series": [{"name": "Попередження та інші", "type": "bar", "stack": "a",
                                               "color": "#f4a261", "data": [r[2] - r[3] - r[4] for r in rep.rows]},
                                              {"name": "Критичні", "type": "bar", "stack": "a", "color": "#d62828",
                                               "data": [r[3] for r in rep.rows]},
                                              {"name": "Недоступність", "type": "bar", "stack": "a", "color": "#6c757d",
                                               "data": [r[4] for r in rep.rows]}]}).classes("w-full h-72")
                    if rep.key == "uptime" and rep.rows:
                        low = sorted(rep.rows, key=lambda r: r[4])[:10]
                        ui.echart({"grid": {"left": 110, "right": 40, "top": 10, "bottom": 30},
                                   "tooltip": {}, "xAxis": {"type": "value", "min": round(min(r[4] for r in low) - 0.1, 1),
                                                            "max": 100},
                                   "yAxis": {"type": "category", "data": [r[0] for r in low]},
                                   "series": [{"type": "bar", "color": "#2a9d8f", "data": [r[4] for r in low],
                                               "label": {"show": True, "position": "right"}}]}).classes("w-full h-64")
                    cols = [{"name": str(i), "label": h, "field": str(i), "align": "left", "sortable": True}
                            for i, h in enumerate(rep.headers)]
                    rows = [{str(i): ("—" if v is None else v) for i, v in enumerate(r)} | {"_k": n}
                            for n, r in enumerate(rep.rows)]
                    ui.table(columns=cols, rows=rows, row_key="_k", pagination=10).classes("w-full")
                    ui.button("CSV цього звіту", icon="download", on_click=lambda rep=rep: ui.download.content(
                        rp.to_csv(rep), f"netguard_{rep.key}.csv", "text/csv")).props("flat")

    def export(fmt: str):
        if not state["reports"]:
            return
        if fmt == "xlsx":
            ui.download.content(rp.to_xlsx(state["reports"], state["period"]), "netguard_report.xlsx",
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            ui.download.content(rp.to_pdf(state["reports"], state["period"]), "netguard_report.pdf", "application/pdf")

    def preset(days: int):
        flt.update({"from": str(today - timedelta(days=days)), "to": str(today)})
        f_in.value, t_in.value = flt["from"], flt["to"]

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("items-end gap-3"):
            f_in = ui.input("Від", value=flt["from"], on_change=lambda e: (flt.update({"from": e.value}), body.refresh())) \
                .props("type=date").classes("w-40")
            t_in = ui.input("До (включно)", value=flt["to"], on_change=lambda e: (flt.update(to=e.value), body.refresh())) \
                .props("type=date").classes("w-40")
            for label, d in (("Доба", 1), ("7 діб", 7), ("30 діб", 30)):
                ui.button(label, on_click=lambda d=d: preset(d)).props("outline dense")
            ui.space()
            ui.button("Excel", icon="table_view", on_click=lambda: export("xlsx")).props("color=positive")
            ui.button("PDF", icon="picture_as_pdf", on_click=lambda: export("pdf")).props("color=negative")
    await body()

"""Сторінка «Запити до БД»: демонстрація роботи бази даних SQL-запитами з результатами.

Каталог запитів і їх виконання — в API Gateway (/api/admin/queries): запити вибірки виконуються
від імені ролі ng_report у транзакції лише для читання; запити, що змінюють дані (тригери, процедури,
порушення обмежень), — у транзакції, яка завжди відкочується, тож стан БД не змінюється.
"""
from nicegui import ui

from . import api
from .common import admin_only, fmt_td, frame, jsonable

SELECT, LOGIC, ERROR = "select", "logic", "error"


def cell(v, pg_type: str | None):
    """Значення клітинки результату за типом стовпця PostgreSQL."""
    if v is None:
        return "NULL"
    if pg_type == "interval":                    # API передає INTERVAL у секундах
        return fmt_td(v)
    v = jsonable(v)
    return round(v, 3) if isinstance(v, float) else v


def show_result(res: dict, kind: str):
    err, ms = res["error"], res["ms"]
    if err is not None:
        expected = kind == ERROR
        with ui.card().classes("w-full p-4 " + ("bg-orange-1" if expected else "bg-red-1")):
            with ui.row().classes("items-center gap-2"):
                ui.icon("gpp_good" if expected else "error").classes("text-2xl " + ("text-deep-orange" if expected
                                                                                     else "text-negative"))
                ui.label("Очікувана помилка: СУБД відхилила операцію" if expected else "Помилка виконання") \
                    .classes("font-semibold")
            ui.label(f"ERROR: {err['message']}").classes("font-mono text-sm mt-1")
            if err["detail"]:
                ui.label(f"DETAIL: {err['detail']}").classes("font-mono text-sm")
            meta = [f"SQLSTATE {err['sqlstate']}"] + ([f"обмеження {err['constraint']}"] if err["constraint"] else []) + \
                   ([f"таблиця {err['table']}"] if err["table"] else [])
            ui.label(" · ".join(meta)).classes("text-xs text-grey-8 mt-1")
            ui.label(f"Транзакцію відкочено · {ms:.0f} мс").classes("text-xs text-grey-7")
        return
    cols, rows = res["columns"], res["rows"]
    if cols is None:
        ui.label(f"Виконано без набору результатів · {ms:.0f} мс").classes("text-sm text-grey-7")
        return
    data = [{"_k": i, **{str(j): cell(v, t) for j, (v, t) in enumerate(zip(r, res["types"]))}}
            for i, r in enumerate(rows)]
    ui.table(columns=[{"name": str(j), "label": c, "field": str(j), "align": "left"} for j, c in enumerate(cols)],
             rows=data, row_key="_k").classes("w-full").props("dense flat bordered :pagination='{rowsPerPage: 0}'"
                                                              " hide-pagination")
    tail = " · транзакцію відкочено, дані не змінено" if kind == LOGIC else ""
    ui.label(f"Рядків: {len(rows)}{' (показано перші 500)' if len(rows) == 500 else ''} · {ms:.0f} мс{tail}") \
        .classes("text-xs text-grey-7")


@ui.page("/queries")
async def queries_page(q: str = "state", run: bool = False):
    await frame("Запити до БД", "Демонстрація роботи бази даних: SQL-запит і результат його виконання в PostgreSQL")
    if not admin_only():
        return
    try:
        catalogue = await api.get("/api/admin/queries")
    except api.ApiError as e:
        ui.label(f"Каталог запитів недоступний: {e.message}").classes("text-negative")
        return
    groups, queries = catalogue["groups"], catalogue["queries"]
    current = {"q": next((x for x in queries if x["key"] == q), queries[0])}

    @ui.refreshable
    async def panel(execute_now: bool = False):
        cq = current["q"]
        with ui.card().classes("w-full p-5 gap-3").props("id=query-panel"):
            with ui.row().classes("items-center gap-2"):
                ui.badge(groups[cq["group"]], color="secondary")
                ui.label(cq["title"]).classes("text-lg font-semibold")
            role = "ng_report, лише читання" if cq["kind"] == SELECT else cq.get("role") or "ng_web"
            ui.label(f"Роль СУБД: {role}").classes("text-xs text-grey-7")
            ui.label(cq["note"]).classes("text-sm text-grey-8")
            ui.code(cq["sql"], language="postgresql").classes("w-full text-[13px]")
            result = ui.column().classes("w-full gap-2")

            async def go():
                result.clear()
                try:
                    res = await api.post(f"/api/admin/queries/{cq['key']}/run")
                except api.ApiError as e:
                    ui.notify(e.message, type="negative")
                    return
                with result:
                    show_result(res, cq["kind"])

            ui.button("Виконати", icon="play_arrow", on_click=go).props("unelevated")
        if execute_now:
            await go()

    with ui.tabs().classes("w-full") as tabs:
        t_demo = ui.tab("Демонстраційні запити")
        t_free = ui.tab("Довільний запит (лише читання)")
    with ui.tab_panels(tabs, value=t_demo).classes("w-full bg-transparent"):
        with ui.tab_panel(t_demo).classes("p-0"):
            with ui.row().classes("w-full gap-4 items-start no-wrap"):
                with ui.card().classes("w-80 p-0 shrink-0"):
                    for gkey, gtitle in groups.items():
                        with ui.expansion(gtitle, value=True).classes("w-full").props("dense header-class=text-primary"):
                            for item in [x for x in queries if x["group"] == gkey]:
                                async def pick(item=item):
                                    current["q"] = item
                                    await panel.refresh(execute_now=False)
                                ui.item(item["title"], on_click=pick).props("clickable dense").classes("text-sm")
                with ui.column().classes("flex-1 min-w-0"):
                    await panel(execute_now=run)
        with ui.tab_panel(t_free).classes("p-0"):
            with ui.card().classes("w-full p-5 gap-3"):
                ui.label("Запит виконується від імені ролі ng_report у транзакції лише для читання; "
                         "тайм-аут — 10 с, показуються перші 500 рядків.").classes("text-sm text-grey-8")
                sql = ui.textarea(value="SELECT name, status, last_seen_at\n  FROM equipment\n ORDER BY last_seen_at DESC\n LIMIT 10;") \
                    .props("outlined autogrow").classes("w-full font-mono")
                out = ui.column().classes("w-full gap-2")

                async def free():
                    out.clear()
                    try:
                        res = await api.post("/api/admin/queries/sql", {"sql": sql.value})
                    except api.ApiError as e:
                        ui.notify(e.message, type="negative")
                        return
                    with out:
                        show_result(res, SELECT)

                ui.button("Виконати", icon="play_arrow", on_click=free).props("unelevated")

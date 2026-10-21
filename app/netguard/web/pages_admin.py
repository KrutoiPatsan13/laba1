"""Сторінки адміністратора: порогові значення (FR-06), користувачі (FR-11), YAML-конфігурація SNMP."""
import json

import yaml
from nicegui import ui
from psycopg.types.json import Jsonb

from ..db import error_text
from ..snmp_yaml import ConfigError, load, to_json
from .common import CATEGORY, ROLE, admin_only, badge, badge_slot, db, fmt_dt, fmt_num, frame, uid

SEV = {"warning": ("Попередження", "warning"), "critical": ("Критично", "negative")}


# ---------------------------------------------------------------------------- порогові значення
@ui.page("/thresholds")
async def thresholds_page():
    await frame("Порогові значення", "Межі норми метрик для типів обладнання; зміни фіксуються в журналі подій")
    if not admin_only():
        return
    types = {r["id"]: f"{r['name']} ({CATEGORY[r['category']]})"
             for r in await db.all("SELECT id, name, category FROM equipment_type WHERE name NOT LIKE '~%%' ORDER BY name")}
    metrics = {r["id"]: f"{r['name']}, {r['unit']}" for r in await db.all("SELECT id, name, unit FROM metric ORDER BY id")}

    @ui.refreshable
    async def table():
        rows = await db.all(
            "SELECT t.*, et.name AS type_name, m.name AS metric_name, m.unit,"
            " (SELECT count(*) FROM incident i WHERE i.threshold_id = t.id) AS incidents"
            " FROM threshold t JOIN equipment_type et ON et.id = t.equipment_type_id JOIN metric m ON m.id = t.metric_id"
            " ORDER BY et.name, m.id, t.severity")
        data = [{"id": r["id"], "type": r["type_name"], "metric": f"{r['metric_name']}, {r['unit']}",
                 "severity_label": SEV[r["severity"]][0], "severity_color": SEV[r["severity"]][1],
                 "min": fmt_num(r["min_value"]), "max": fmt_num(r["max_value"]),
                 "active": "так" if r["is_active"] else "ні", "incidents": r["incidents"],
                 "updated": fmt_dt(r["updated_at"]), "raw": {k: r[k] for k in ("equipment_type_id", "metric_id",
                                                                              "severity", "is_active")} |
                 {"min_value": float(r["min_value"]) if r["min_value"] is not None else None,
                  "max_value": float(r["max_value"]) if r["max_value"] is not None else None}} for r in rows]
        t = ui.table(columns=[{"name": "type", "label": "Тип обладнання", "field": "type", "align": "left"},
                              {"name": "metric", "label": "Метрика", "field": "metric", "align": "left"},
                              {"name": "severity", "label": "Рівень", "field": "severity_label", "align": "left"},
                              {"name": "min", "label": "Мінімум", "field": "min"},
                              {"name": "max", "label": "Максимум", "field": "max"},
                              {"name": "active", "label": "Діє", "field": "active"},
                              {"name": "incidents", "label": "Інцидентів", "field": "incidents"},
                              {"name": "updated", "label": "Змінено", "field": "updated", "align": "left"}],
                     rows=data, row_key="id", pagination=20).classes("w-full")
        badge_slot(t, "severity")
        t.on("rowClick", lambda e: edit(e.args[1]))

    def edit(row: dict | None):
        raw = (row or {}).get("raw", {})
        with ui.dialog() as dlg, ui.card().classes("w-[520px] p-6 gap-3"):
            ui.label("Поріг" if row else "Новий поріг").classes("text-lg font-semibold")
            t_sel = ui.select(types, label="Тип обладнання", value=raw.get("equipment_type_id")).props("outlined") \
                .classes("w-full")
            m_sel = ui.select(metrics, label="Метрика", value=raw.get("metric_id")).props("outlined").classes("w-full")
            sev = ui.select({k: v[0] for k, v in SEV.items()}, label="Рівень критичності",
                            value=raw.get("severity", "warning")).props("outlined").classes("w-full")
            with ui.row().classes("w-full gap-3"):
                lo = ui.number("Мінімум (порушення, якщо менше)", value=raw.get("min_value")).props("outlined").classes("flex-1")
                hi = ui.number("Максимум (порушення, якщо більше)", value=raw.get("max_value")).props("outlined").classes("flex-1")
            active = ui.switch("Поріг діє", value=raw.get("is_active", True))

            async def save():
                p = {"t": t_sel.value, "m": m_sel.value, "s": sev.value, "lo": lo.value, "hi": hi.value,
                     "a": active.value, "id": row["id"] if row else None}
                try:
                    if row:
                        await db.run("UPDATE threshold SET equipment_type_id = %(t)s, metric_id = %(m)s, severity = %(s)s,"
                                     " min_value = %(lo)s, max_value = %(hi)s, is_active = %(a)s WHERE id = %(id)s",
                                     p, user_id=uid())
                    else:
                        await db.run("INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value, max_value,"
                                     " is_active) VALUES (%(t)s, %(m)s, %(s)s, %(lo)s, %(hi)s, %(a)s)", p, user_id=uid())
                except Exception as e:
                    ui.notify(f"Не збережено: {error_text(e)}", type="negative", multi_line=True)
                    return
                ui.notify("Поріг збережено; зміну записано в журнал подій", type="positive")
                dlg.close()
                await table.refresh()

            async def delete():
                try:
                    await db.run("DELETE FROM threshold WHERE id = %s", (row["id"],), user_id=uid())
                except Exception as e:
                    ui.notify(f"Поріг не видалено: {error_text(e)}. Деактивуйте його замість видалення.",
                              type="negative", multi_line=True)
                    return
                ui.notify("Поріг видалено", type="positive")
                dlg.close()
                await table.refresh()

            with ui.row().classes("w-full justify-between"):
                if row:
                    ui.button("Видалити", icon="delete", on_click=delete).props("flat color=negative")
                else:
                    ui.space()
                with ui.row().classes("gap-2"):
                    ui.button("Скасувати", on_click=dlg.close).props("flat")
                    ui.button("Зберегти", icon="save", on_click=save)
        dlg.open()

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Клацніть рядок, щоб змінити поріг").classes("text-sm text-grey-7")
            ui.space()
            ui.button("Додати поріг", icon="add", on_click=lambda: edit(None))
        await table()


# ---------------------------------------------------------------------------- користувачі
@ui.page("/users")
async def users_page():
    await frame("Користувачі", "Облікові записи інженерів техпідтримки й адміністраторів; паролі зберігаються як bcrypt-хеші")
    if not admin_only():
        return
    roles = {r["id"]: r["name"] for r in await db.all("SELECT id, name FROM roles ORDER BY id")}

    @ui.refreshable
    async def table():
        rows = await db.all("SELECT u.*, r.code AS role_code FROM users u JOIN roles r ON r.id = u.role_id ORDER BY u.full_name")
        data = [{"id": r["id"], "login": r["login"], "name": r["full_name"], "role": ROLE[r["role_code"]],
                 "email": r["email"] or "—", "telegram": str(r["telegram_chat_id"] or "—"),
                 "active_label": "активний" if r["is_active"] else "заблоковано",
                 "active_color": "positive" if r["is_active"] else "negative",
                 "last": fmt_dt(r["last_login_at"]), "created": fmt_dt(r["created_at"]),
                 "raw": {"role_id": r["role_id"], "full_name": r["full_name"], "login": r["login"], "email": r["email"],
                         "telegram_chat_id": r["telegram_chat_id"], "is_active": r["is_active"]}} for r in rows]
        t = ui.table(columns=[{"name": "name", "label": "ПІБ", "field": "name", "align": "left", "sortable": True},
                              {"name": "login", "label": "Логін", "field": "login", "align": "left"},
                              {"name": "role", "label": "Роль", "field": "role", "align": "left"},
                              {"name": "email", "label": "E-mail", "field": "email", "align": "left"},
                              {"name": "telegram", "label": "Telegram chat id", "field": "telegram", "align": "left"},
                              {"name": "active", "label": "Стан", "field": "active_label", "align": "left"},
                              {"name": "last", "label": "Останній вхід", "field": "last", "align": "left"},
                              {"name": "created", "label": "Створено", "field": "created", "align": "left"}],
                     rows=data, row_key="id").classes("w-full").props(":pagination='{rowsPerPage: 0}' hide-pagination")
        badge_slot(t, "active")
        t.on("rowClick", lambda e: edit(e.args[1]))

    def edit(row: dict | None):
        raw = (row or {}).get("raw", {})
        with ui.dialog() as dlg, ui.card().classes("w-[520px] p-6 gap-3"):
            ui.label(f"Користувач {raw['login']}" if row else "Новий користувач").classes("text-lg font-semibold")
            name = ui.input("ПІБ", value=raw.get("full_name", "")).props("outlined").classes("w-full")
            login = ui.input("Логін (латиниця, цифри, «_», «.»)", value=raw.get("login", "")).props("outlined") \
                .classes("w-full")
            role = ui.select(roles, label="Роль", value=raw.get("role_id", min(roles))).props("outlined").classes("w-full")
            email = ui.input("E-mail", value=raw.get("email") or "").props("outlined").classes("w-full")
            tg = ui.input("Telegram chat id", value=str(raw.get("telegram_chat_id") or "")).props("outlined").classes("w-full")
            pwd = ui.input("Новий пароль" if row else "Пароль", password=True, password_toggle_button=True) \
                .props("outlined").classes("w-full")
            if row:
                ui.label("Залиште поле пароля порожнім, щоб не змінювати пароль").classes("text-xs text-grey-6")
            active = ui.switch("Обліковий запис активний", value=raw.get("is_active", True))

            async def save():
                if not row and len(pwd.value or "") < 8:
                    ui.notify("Пароль має містити щонайменше 8 символів", type="warning")
                    return
                if row and row["id"] == uid() and not active.value:
                    ui.notify("Не можна заблокувати власний обліковий запис", type="warning")
                    return
                p = {"name": name.value.strip(), "login": login.value.strip().lower(), "role": role.value,
                     "email": (email.value or "").strip().lower() or None,
                     "tg": int(tg.value) if (tg.value or "").strip().lstrip("-").isdigit() else None,
                     "a": active.value, "pwd": pwd.value or None, "id": row["id"] if row else None}
                try:
                    if row:
                        await db.run("UPDATE users SET full_name = %(name)s, login = %(login)s, role_id = %(role)s,"
                                     " email = %(email)s, telegram_chat_id = %(tg)s, is_active = %(a)s,"
                                     " password_hash = coalesce(fn_hash_password(%(pwd)s), password_hash) WHERE id = %(id)s",
                                     p, user_id=uid())
                    else:
                        await db.run("INSERT INTO users (full_name, login, role_id, email, telegram_chat_id, is_active,"
                                     " password_hash) VALUES (%(name)s, %(login)s, %(role)s, %(email)s, %(tg)s, %(a)s,"
                                     " fn_hash_password(%(pwd)s))", p, user_id=uid())
                except Exception as e:
                    ui.notify(f"Не збережено: {error_text(e)}", type="negative", multi_line=True)
                    return
                ui.notify("Збережено", type="positive")
                dlg.close()
                await table.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Скасувати", on_click=dlg.close).props("flat")
                ui.button("Зберегти", icon="save", on_click=save)
        dlg.open()

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Клацніть рядок, щоб змінити користувача, роль, пароль або заблокувати доступ") \
                .classes("text-sm text-grey-7")
            ui.space()
            ui.button("Додати користувача", icon="person_add", on_click=lambda: edit(None))
        await table()


# ---------------------------------------------------------------------------- конфігурація SNMP
@ui.page("/snmp")
async def snmp_page():
    await frame("Конфігурація SNMP", "Версії YAML-файлу конфігурації опитування (ЛР4 ТСПП): застосування, перегляд,"
                                     " порівняння, повернення до попередньої версії")
    if not admin_only():
        return

    async def apply(cfg: dict, sha: str, path: str):
        try:
            res = await db.one("SELECT sp_apply_snmp_config(%s, %s, %s, %s) AS r",
                               (path, sha, Jsonb(cfg), uid()), user_id=uid())
        except Exception as e:
            ui.notify(f"Конфігурацію не застосовано, активною лишилася попередня версія: {error_text(e)}",
                      type="negative", multi_line=True, timeout=12000)
            return
        msg = {"skipped": "Ця версія вже активна — змін немає", "reactivated": "Раніше застосовану версію активовано знову",
               "applied": "Нову версію конфігурації застосовано"}[res["r"]]
        ui.notify(msg, type="positive" if res["r"] != "skipped" else "info")
        await versions.refresh()
        await active_summary.refresh()

    async def on_upload(e):
        name = e.file.name
        if not name.lower().endswith((".yaml", ".yml")):
            ui.notify("Потрібен файл .yaml або .yml", type="warning")
            return
        raw = await e.file.read()
        try:
            cfg, sha = load(raw)
        except ConfigError as err:
            ui.notify(f"Помилка у файлі {name}: {err}", type="negative", multi_line=True, timeout=15000)
            return
        await apply(cfg, sha, f"/etc/netguard/{name}")
        uploader.reset()

    def show(v: dict):
        with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full p-6 gap-2"):
            ui.label(f"Версія №{v['id']} · {fmt_dt(v['loaded_at'])}").classes("text-lg font-semibold")
            ui.label(f"SHA-256: {v['sha256']}").classes("text-xs text-grey-7 font-mono")
            ui.code(yaml.safe_dump(v["content"], allow_unicode=True, sort_keys=False), language="yaml") \
                .classes("w-full max-h-[60vh] overflow-auto")
            ui.button("Закрити", on_click=dlg.close).props("flat").classes("self-end")
        dlg.open()

    async def compare(v: dict):
        cur = await db.one("SELECT content FROM snmp_config WHERE is_active")
        diff = config_diff(cur["content"] if cur else {}, v["content"])
        with ui.dialog() as dlg, ui.card().classes("w-[680px] max-w-full p-6 gap-2"):
            ui.label(f"Версія №{v['id']} порівняно з активною").classes("text-lg font-semibold")
            if not diff:
                ui.label("Відмінностей немає").classes("text-grey-7")
            for line in diff:
                ui.label(line).classes("text-sm font-mono")
            ui.button("Закрити", on_click=dlg.close).props("flat").classes("self-end")
        dlg.open()

    @ui.refreshable
    async def versions():
        rows = await db.all(
            "SELECT c.*, u.full_name, (SELECT count(*) FROM equipment e WHERE e.config_id = c.id) AS devices,"
            " jsonb_array_length(c.content -> 'devices') AS in_file FROM snmp_config c"
            " LEFT JOIN users u ON u.id = c.loaded_by ORDER BY c.id DESC")
        for v in rows:
            with ui.card().classes("w-full p-4"):
                with ui.row().classes("items-center w-full gap-4"):
                    ui.label(f"Версія №{v['id']}").classes("text-lg font-semibold")
                    if v["is_active"]:
                        badge("активна", "positive")
                    ui.label(f"застосовано {fmt_dt(v['loaded_at'])} · {v['full_name'] or 'Polling Service'}") \
                        .classes("text-sm text-grey-7")
                    ui.space()
                    ui.button("Переглянути", icon="visibility", on_click=lambda v=v: show(v)).props("flat dense")
                    if not v["is_active"]:
                        ui.button("Порівняти", icon="compare_arrows", on_click=lambda v=v: compare(v)).props("flat dense")
                        ui.button("Повернути", icon="restore",
                                  on_click=lambda v=v: apply(v["content"], v["sha256"], v["file_path"])) \
                            .props("outline dense color=secondary").tooltip("Зробити цю версію знову активною")
                with ui.row().classes("gap-6 text-sm"):
                    ui.label(f"Файл: {v['file_path']}")
                    ui.label(f"SHA-256: {v['sha256'][:16]}…").classes("font-mono")
                    ui.label(f"пристроїв у файлі: {v['in_file']}, синхронізовано цією версією: {v['devices']}")
                    ui.label(f"профілів: {len(v['content'].get('profiles', {}))}, облікових записів:"
                             f" {len(v['content'].get('credentials', {}))}")

    @ui.refreshable
    async def active_summary():
        creds = await db.all("SELECT name, snmp_version, username, auth_protocol, priv_protocol, secret_ref,"
                             " (SELECT count(*) FROM equipment e WHERE e.credential_id = c.id AND e.is_active) AS devices"
                             " FROM snmp_credential c ORDER BY name")
        plan = await db.all("SELECT t.snmp_profile, t.name, m.code, om.oid, om.scale FROM snmp_oid_map om"
                            " JOIN equipment_type t ON t.id = om.equipment_type_id JOIN metric m ON m.id = om.metric_id"
                            " ORDER BY t.snmp_profile, m.id")
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("flex-[1.2] min-w-[440px] p-4"):
                ui.label("Облікові дані SNMP (секретів у БД немає)").classes("font-semibold")
                ui.table(columns=[{"name": c, "label": l, "field": c, "align": "left"} for c, l in
                                  [("name", "Ім'я"), ("ver", "Версія"), ("usm", "USM (v3)"), ("secret_ref", "Змінна з секретом"),
                                   ("devices", "Пристроїв")]],
                         rows=[{"name": c["name"], "ver": c["snmp_version"], "secret_ref": c["secret_ref"],
                                "usm": " / ".join(x for x in (c["username"], c["auth_protocol"], c["priv_protocol"]) if x) or "—",
                                "devices": c["devices"]} for c in creds], row_key="name") \
                    .classes("w-full").props("flat dense hide-pagination wrap-cells")
            with ui.card().classes("flex-1 min-w-[440px] p-4"):
                ui.label("Шаблони опитування: метрика → OID").classes("font-semibold")
                ui.table(columns=[{"name": c, "label": l, "field": c, "align": "left"} for c, l in
                                  [("profile", "Профіль"), ("code", "Метрика"), ("oid", "OID"), ("scale", "Множник")]],
                         rows=[{"id": f"{p['snmp_profile']}/{p['code']}", "profile": p["snmp_profile"], "code": p["code"],
                                "oid": p["oid"], "scale": fmt_num(p["scale"], 6)} for p in plan], row_key="id",
                         pagination=8).classes("w-full").props("flat dense")

    with ui.card().classes("w-full p-4"):
        with ui.row().classes("items-center gap-4 w-full"):
            ui.icon("upload_file").classes("text-3xl text-primary")
            with ui.column().classes("gap-0"):
                ui.label("Застосувати новий YAML-файл").classes("font-semibold")
                ui.label("Файл суворо перевіряється; помилка у файлі чи в БД не змінює активну конфігурацію") \
                    .classes("text-sm text-grey-7")
            ui.space()
            uploader = ui.upload(label="Оберіть YAML-файл", on_upload=on_upload, auto_upload=True, max_files=1) \
                .props("accept=.yaml,.yml flat bordered color=secondary").classes("w-80")
    ui.label("Активна конфігурація").classes("text-lg font-semibold")
    await active_summary()
    ui.label("Історія версій").classes("text-lg font-semibold")
    await versions()


def config_diff(old: dict, new: dict) -> list[str]:
    """Короткий перелік відмінностей двох версій конфігурації (пристрої, профілі, облікові дані)."""
    out = []
    od = {d["name"]: d for d in old.get("devices", [])}
    nd = {d["name"]: d for d in new.get("devices", [])}
    out += [f"+ пристрій {n} ({nd[n]['ip']}, {nd[n]['location']})" for n in sorted(nd.keys() - od.keys())]
    out += [f"− пристрій {n} ({od[n]['ip']})" for n in sorted(od.keys() - nd.keys())]
    for n in sorted(nd.keys() & od.keys()):
        changes = [f"{k}: {od[n].get(k)} → {nd[n].get(k)}" for k in sorted(set(od[n]) | set(nd[n]))
                   if od[n].get(k) != nd[n].get(k)]
        if changes:
            out.append(f"~ пристрій {n}: " + ", ".join(changes))
    for sec, title in (("profiles", "профіль"), ("credentials", "обліковий запис")):
        o, n_ = old.get(sec, {}), new.get(sec, {})
        out += [f"+ {title} {k}" for k in sorted(n_.keys() - o.keys())]
        out += [f"− {title} {k}" for k in sorted(o.keys() - n_.keys())]
        out += [f"~ {title} {k}" for k in sorted(n_.keys() & o.keys()) if json.dumps(n_[k], sort_keys=True) !=
                json.dumps(o[k], sort_keys=True)]
    if old.get("defaults") != new.get("defaults"):
        out.append(f"~ defaults: {old.get('defaults')} → {new.get('defaults')}")
    return out


__all__ = ["thresholds_page", "users_page", "snmp_page", "to_json"]

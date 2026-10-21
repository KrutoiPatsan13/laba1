"""Знімки екрана веб-інтерфейсу NetGuard Monitor для звіту (docs/screenshots/*.png).

Потрібні запущені сервіси (python -m netguard) і демо-БД (python -m netguard.demo_setup --reset),
а також Playwright з Chromium:

    cd app && NETGUARD_DSN=... python ../tools/screenshots.py [http://localhost:8080]
"""
import asyncio
import os
import sys
from pathlib import Path

import psycopg
from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
CHROME = os.environ.get("CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
QUERIES = ["state", "plan", "open", "history", "lateral", "mtta", "uptime", "ups", "partitions", "events",
           "trigger", "offline", "ack", "auth", "yaml", "check", "unique", "fk", "transition", "role"]


def ids() -> dict:
    with psycopg.connect(os.environ.get("NETGUARD_DSN", "dbname=netguard")) as c:
        c.execute("SET search_path TO netguard")
        q = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "ups": q("SELECT id FROM equipment WHERE name = 'UPS-TRO-01'"),
            "closed": q("SELECT i.id FROM incident i JOIN threshold t ON t.id = i.threshold_id JOIN metric m"
                        " ON m.id = t.metric_id JOIN equipment e ON e.id = i.equipment_id"
                        " WHERE e.name = 'UPS-TRO-01' AND m.code = 'battery_charge' AND i.status = 'closed'"),
            "open": q("SELECT id FROM incident WHERE status IN ('new', 'escalated')"
                      " ORDER BY incident_type = 'threshold' DESC, opened_at DESC LIMIT 1"),
        }


async def login(ctx, user: str, password: str):
    pg = await ctx.new_page()
    await pg.goto(f"{BASE}/login")
    await pg.wait_for_timeout(800)
    await pg.screenshot(path=OUT / "01_login.png")
    await pg.fill("input[aria-label='Логін']", user)
    await pg.fill("input[aria-label='Пароль']", password)
    await pg.click("text=Увійти")
    await pg.wait_for_url(f"{BASE}/", timeout=10000)
    return pg


async def shot(pg, path: str, name: str, max_h: int = 1700, wait: int = 2500):
    await pg.set_viewport_size({"width": 1440, "height": 900})
    await pg.goto(BASE + path)
    await pg.wait_for_timeout(wait)
    h = await pg.evaluate("document.documentElement.scrollHeight")
    await pg.set_viewport_size({"width": 1440, "height": min(max(h, 900), max_h)})
    await pg.wait_for_timeout(600)
    await pg.screenshot(path=OUT / f"{name}.png")
    print("✓", name)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    i = ids()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        opts = dict(viewport={"width": 1440, "height": 900}, timezone_id="Europe/Kyiv", locale="uk-UA",
                    device_scale_factor=1.5)
        admin = await login(await browser.new_context(**opts), "admin", "Admin#2026")
        # висота знімків обмежена, щоб у звіті на сторінці вміщалося два рисунки
        await shot(admin, "/", "02_dashboard", 1000)
        await shot(admin, "/equipment", "03_equipment", 900)
        await shot(admin, f"/equipment/{i['ups']}?period=7d", "04_equipment_card", 1050, 3500)
        await shot(admin, "/incidents?status=all&days=7", "05_incidents", 900)
        await shot(admin, f"/incidents/{i['closed']}", "06_incident_card", 900, 3000)
        await shot(admin, "/thresholds", "08_thresholds", 900)
        await shot(admin, "/snmp", "09_snmp", 950)
        await shot(admin, "/users", "10_users", 800)
        await shot(admin, "/events", "11_events", 900)
        await shot(admin, "/reports", "12_reports", 1000, 3500)
        for n, key in enumerate(QUERIES, 1):
            await admin.set_viewport_size({"width": 1440, "height": 1800})
            await admin.goto(f"{BASE}/queries?q={key}&run=true")
            await admin.wait_for_timeout(2500)
            await admin.locator("#query-panel").screenshot(path=OUT / f"q{n:02d}_{key}.png")
            print("✓ запит", key)

        support = await login(await browser.new_context(**opts), "a.koval", "Support#2026")
        if i["open"]:
            await shot(support, f"/incidents/{i['open']}", "07_incident_open", 900)
        await shot(support, "/notifications", "13_notifications", 850)
        await shot(support, "/users", "14_access_denied", 900)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

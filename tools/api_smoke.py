"""Перевірка захисту REST API шлюзу NetGuard Monitor.

Маршрути беруться з опису OpenAPI, тож новий маршрут перевіряється автоматично:
  * без токена кожен маршрут /api/... (крім входу) має відповідати 401;
  * для ролі «Сапорт» маршрути адміністратора мають відповідати 403, а решта GET-маршрутів — 200;
  * адміністратор отримує 200 на всіх GET-маршрутах без параметрів шляху.
Потрібні запущені API Gateway і Reporting Service та демо-БД:

    python tools/api_smoke.py [http://localhost:8081]
"""
import re
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081").rstrip("/")
PUBLIC = {("POST", "/api/auth/login")}
ADMIN_TAGS = {"thresholds", "users", "snmp", "queries"}
SAMPLE = {"from": "2026-01-01T00:00:00+02:00", "to": "2030-01-01T00:00:00+02:00", "fmt": "xlsx"}


def token(c: httpx.Client, login: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"login": login, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def main() -> int:
    failures = []
    with httpx.Client(base_url=BASE, timeout=60) as c:
        spec = c.get("/openapi.json").json()
        ops = [(m.upper(), p, set(o.get("tags", [])), o) for p, item in spec["paths"].items()
               for m, o in item.items() if p.startswith("/api/")]
        auth = {"admin": token(c, "admin", "Admin#2026"), "support": token(c, "a.koval", "Support#2026")}

        def call(method, path, tok=None, op=None):
            params = {p["name"]: SAMPLE[p["name"]] for p in (op or {}).get("parameters", [])
                      if p.get("in") == "query" and p.get("required") and p["name"] in SAMPLE}
            headers = {"Authorization": f"Bearer {tok}"} if tok else {}
            return c.request(method, re.sub(r"\{[^}]+\}", "1", path), headers=headers, params=params,
                             json={} if method in ("POST", "PUT", "PATCH") else None)

        for method, path, tags, op in ops:
            if (method, path) in PUBLIC:
                continue
            r = call(method, path, op=op)
            if r.status_code != 401:
                failures.append(f"{method} {path} без токена: {r.status_code}, очікувалось 401")
            # зміни обладнання дозволені лише адміністратору, перегляд — обом ролям
            admin_only = (bool(tags & ADMIN_TAGS) or path.startswith("/api/admin")
                          or ("equipment" in tags and method != "GET"))
            if admin_only:
                r = call(method, path, auth["support"], op)
                if r.status_code != 403:
                    failures.append(f"{method} {path} роль «Сапорт»: {r.status_code}, очікувалось 403")
            if method == "GET" and "{" not in path:
                for role in (("admin",) if admin_only else ("admin", "support")):
                    r = call(method, path, auth[role], op)
                    if r.status_code != 200:
                        failures.append(f"GET {path} роль {role}: {r.status_code} {r.text[:120]}")
        print(f"перевірено маршрутів: {len(ops)}")
    for f in failures:
        print("FAIL", f)
    print("усі перевірки пройдено" if not failures else f"помилок: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Застосування YAML-конфігурації SNMP до БД NetGuard Monitor (прототип).

Читає config/snmp_config.yaml, перевіряє його та генерує SQL-скрипт, який в одній
транзакції реєструє нову версію файлу (snmp_config) і синхронізує snmp_credential,
equipment_type, snmp_oid_map та equipment. Якщо файл із такою контрольною сумою
вже застосовано, скрипт нічого не змінює. Будь-яка помилка відкочує транзакцію,
і активною лишається попередня версія.

usage:
    python3 tools/snmp_config_sync.py config/snmp_config.yaml | psql -d netguard

У лабораторній роботі №2 цю логіку буде перенесено в збережену процедуру
sp_apply_snmp_config(path, sha256, content jsonb).
"""
import hashlib
import json
import re
import sys

import yaml

src = sys.argv[1]
container_path = sys.argv[2] if len(sys.argv) > 2 else "/etc/netguard/snmp_config.yaml"
raw = open(src, "rb").read()
sha = hashlib.sha256(raw).hexdigest()
cfg = yaml.safe_load(raw)

SECRET = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


assert isinstance(cfg.get("version"), int), "version: ціле число"
for k in ("defaults", "credentials", "profiles", "devices"):
    assert k in cfg, f"відсутній розділ {k}"
d = cfg["defaults"]

out = ["\\set ON_ERROR_STOP 1", "SET search_path TO netguard;", "BEGIN;",
       f"SELECT EXISTS (SELECT 1 FROM snmp_config WHERE sha256 = {q(sha)}) AS skip \\gset",
       "\\if :skip", "\\echo 'конфігурація з такою контрольною сумою вже застосована — пропуск'", "ROLLBACK;", "\\quit", "\\endif",
       "UPDATE snmp_config SET is_active = FALSE WHERE is_active;",
       f"INSERT INTO snmp_config (file_path, sha256, format_version, content, is_active) VALUES "
       f"({q(container_path)}, {q(sha)}, {cfg['version']}, {q(json.dumps(cfg, ensure_ascii=False))}::jsonb, TRUE);",
       "CREATE TEMP TABLE cur ON COMMIT DROP AS SELECT id FROM snmp_config WHERE is_active;"]

# credentials -> snmp_credential (upsert за name)
for name, c in cfg["credentials"].items():
    m = SECRET.match(str(c.get("secret", "")))
    assert m, f"credentials.{name}.secret має бути посиланням ${{VAR}}, а не відкритим секретом"
    out.append(
        "INSERT INTO snmp_credential (config_id, name, snmp_version, username, auth_protocol, priv_protocol, secret_ref) "
        f"SELECT id, {q(name)}, {q(c['version'])}, {q(c.get('username'))}, {q(c.get('auth_protocol'))}, "
        f"{q(c.get('priv_protocol'))}, {q(m.group(1))} FROM cur "
        "ON CONFLICT (name) DO UPDATE SET config_id = EXCLUDED.config_id, snmp_version = EXCLUDED.snmp_version, "
        "username = EXCLUDED.username, auth_protocol = EXCLUDED.auth_protocol, "
        "priv_protocol = EXCLUDED.priv_protocol, secret_ref = EXCLUDED.secret_ref;")

# profiles -> equipment_type (upsert за snmp_profile) + snmp_oid_map (повна заміна для профілю)
for key, p in cfg["profiles"].items():
    out.append(
        "INSERT INTO equipment_type (snmp_profile, name, category, vendor, snmp_mib) "
        f"VALUES ({q(key)}, {q(p['name'])}, {q(p['category'])}, {q(p.get('vendor'))}, {q(p.get('mib'))}) "
        "ON CONFLICT (snmp_profile) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category, "
        "vendor = EXCLUDED.vendor, snmp_mib = EXCLUDED.snmp_mib;")
    out.append("DELETE FROM snmp_oid_map WHERE equipment_type_id = "
               f"(SELECT id FROM equipment_type WHERE snmp_profile = {q(key)});")
    for code, mm in p["metrics"].items():
        # невідомий код метрики -> metric_id IS NULL -> порушення NOT NULL -> відкат транзакції
        out.append(
            "INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, scale, config_id) SELECT "
            f"(SELECT id FROM equipment_type WHERE snmp_profile = {q(key)}), "
            f"(SELECT id FROM metric WHERE code = {q(code)}), {q(str(mm['oid']))}, {mm.get('scale', 1)}, id FROM cur;")

# devices -> equipment (upsert за ip_address); пристрої, зниклі з файлу, — деактивуються
for dev in cfg["devices"]:
    out.append(
        "INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, config_id, snmp_port, poll_interval_s) SELECT "
        f"(SELECT id FROM equipment_type WHERE snmp_profile = {q(dev['profile'])}), "
        f"(SELECT id FROM locations WHERE name = {q(dev['location'])}), {q(dev['name'])}, {q(dev['ip'])}, "
        f"(SELECT id FROM snmp_credential WHERE name = {q(dev['credential'])}), id, "
        f"{dev.get('port', d['port'])}, {dev.get('poll_interval_s', d['poll_interval_s'])} FROM cur "
        "ON CONFLICT (ip_address) DO UPDATE SET type_id = EXCLUDED.type_id, location_id = EXCLUDED.location_id, "
        "name = EXCLUDED.name, credential_id = EXCLUDED.credential_id, config_id = EXCLUDED.config_id, "
        "snmp_port = EXCLUDED.snmp_port, poll_interval_s = EXCLUDED.poll_interval_s, is_active = TRUE;")
ips = ", ".join(q(x["ip"]) + "::inet" for x in cfg["devices"])
out.append(f"UPDATE equipment SET is_active = FALSE WHERE config_id IS NOT NULL AND ip_address NOT IN ({ips});")
# облікові дані, яких немає у файлі, видаляються (RESTRICT не дасть, якщо на них посилаються пристрої)
names = ", ".join(q(n) for n in cfg["credentials"])
out.append(f"DELETE FROM snmp_credential WHERE name NOT IN ({names});")
out.append("INSERT INTO event_log (event_type, message, details) SELECT 'config_change', "
           "'Застосовано YAML-конфігурацію SNMP', jsonb_build_object('snmp_config_id', id, 'sha256', "
           f"{q(sha)}) FROM cur;")
out.append("COMMIT;")
print("\n".join(out))

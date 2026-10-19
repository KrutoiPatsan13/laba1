"""Застосування YAML-конфігурації SNMP до БД NetGuard Monitor (прототип).

Читає config/snmp_config.yaml, суворо перевіряє його й друкує SQL-скрипт для psql, який
в одній транзакції реєструє версію файлу (snmp_config) і синхронізує snmp_credential,
equipment_type, snmp_oid_map та equipment:

* файл, чия контрольна сума збігається з активною версією, — пропуск, нічого не змінюється;
* файл, що вже застосовувався раніше (та сама контрольна сума, версія неактивна), —
  ця версія знову стає активною, і таблиці синхронізуються з її вмістом;
* пристрій ідентифікується за name: зміна IP оновлює наявний запис (історія зберігається),
  а IP, зайнятий іншим активним пристроєм, — помилка й відкат;
* пристрої, яких більше немає у файлі, деактивуються (is_active = FALSE), не видаляються;
  їхні IP-адреси можна віддати новим пристроям (перейменування = новий запис);
* облікові записи, яких немає у файлі, видаляються лише тоді, коли на них не посилається
  жоден пристрій;
* будь-яка помилка в БД відкочує транзакцію — активною лишається попередня версія.

usage:
    set -o pipefail
    python3 tools/snmp_config_sync.py config/snmp_config.yaml | psql -d netguard -v ON_ERROR_STOP=1

Помилка у файлі — повідомлення в stderr і код виходу 2 (SQL не друкується).
У лабораторній роботі №2 цю логіку буде перенесено в збережену процедуру
sp_apply_snmp_config(path, sha256, content jsonb).
"""
import hashlib
import ipaddress
import json
import re
import sys
from decimal import Decimal, InvalidOperation

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
METRIC_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SECRET_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
OID_RE = re.compile(r"^\.?[0-2](\.[0-9]+)+$")
SECTIONS = {"version", "defaults", "credentials", "profiles", "devices"}
DEFAULT_KEYS = {"port", "poll_interval_s", "timeout_ms", "retries", "offline_after_failures"}
CREDENTIAL_KEYS = {"version", "username", "auth_protocol", "priv_protocol", "secret"}
PROFILE_KEYS = {"name", "category", "vendor", "mib", "metrics"}
METRIC_KEYS = {"oid", "scale"}
DEVICE_KEYS = {"name", "ip", "location", "profile", "credential", "port", "poll_interval_s"}
SNMP_VERSIONS = {"v1", "v2c", "v3"}
AUTH = {"MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"}
PRIV = {"DES", "AES", "AES192", "AES256"}
CATEGORIES = {"switch", "router", "onu", "ups"}


class ConfigError(Exception):
    pass


class StrictLoader(yaml.SafeLoader):
    """SafeLoader, що відмовляє на дублікатах ключів (звичайний мовчки бере останнє значення)."""


def _mapping(loader, node, deep=False):
    keys = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise ConfigError(f"рядок {key_node.start_mark.line + 1}: ключ «{key}» повторюється")
        keys.add(key)
    return loader.construct_mapping(node, deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def need(cond, msg):
    if not cond:
        raise ConfigError(msg)


def check_keys(obj, allowed, where):
    need(isinstance(obj, dict), f"{where}: очікується словник")
    extra = set(obj) - allowed
    need(not extra, f"{where}: невідомі ключі {sorted(map(str, extra))} (дозволено: {sorted(allowed)})")


def as_int(value, where, lo, hi):
    need(isinstance(value, int) and not isinstance(value, bool), f"{where}: очікується ціле число")
    need(lo <= value <= hi, f"{where}: значення {value} поза межами {lo}–{hi}")
    return value


def as_text(value, where):
    need(isinstance(value, str) and value.strip() == value and value != "",
         f"{where}: очікується непорожній рядок без пробілів на краях (за потреби — у лапках)")
    return value


def validate(cfg):
    need(isinstance(cfg, dict), "файл має містити словник верхнього рівня")
    need(set(cfg) == SECTIONS, f"розділи верхнього рівня мають бути рівно {sorted(SECTIONS)}, є {sorted(map(str, cfg))}")
    as_int(cfg["version"], "version", 1, 32767)

    d = cfg["defaults"]
    check_keys(d, DEFAULT_KEYS, "defaults")
    need({"port", "poll_interval_s"} <= set(d), "defaults: обов'язкові port і poll_interval_s")
    as_int(d["port"], "defaults.port", 1, 65535)
    as_int(d["poll_interval_s"], "defaults.poll_interval_s", 10, 60)
    for k in ("timeout_ms", "retries", "offline_after_failures"):
        if k in d:
            as_int(d[k], f"defaults.{k}", 0, 600000)

    creds = cfg["credentials"]
    need(isinstance(creds, dict) and creds, "credentials: потрібен непорожній словник")
    for name, c in creds.items():
        where = f"credentials.{name}"
        need(isinstance(name, str) and NAME_RE.match(name), f"{where}: ім'я — малі латинські літери, цифри, «-»")
        check_keys(c, CREDENTIAL_KEYS, where)
        need(c.get("version") in SNMP_VERSIONS, f"{where}.version: одне з {sorted(SNMP_VERSIONS)}")
        need(isinstance(c.get("secret"), str) and SECRET_RE.match(c["secret"]),
             f"{where}.secret: лише посилання на змінну середовища ${{ІМ'Я}}, не відкритий секрет")
        if c["version"] == "v3":
            as_text(c.get("username"), f"{where}.username")
            need(c.get("auth_protocol", "SHA") in AUTH, f"{where}.auth_protocol: одне з {sorted(AUTH)}")
            need(c.get("priv_protocol") is None or c["priv_protocol"] in PRIV, f"{where}.priv_protocol: одне з {sorted(PRIV)}")
            need(c.get("priv_protocol") is None or c.get("auth_protocol") is not None,
                 f"{where}: шифрування (priv_protocol) можливе лише разом з auth_protocol")
        else:
            need(not ({"username", "auth_protocol", "priv_protocol"} & set(c)),
                 f"{where}: username/auth_protocol/priv_protocol дозволені лише для v3")

    profiles = cfg["profiles"]
    need(isinstance(profiles, dict) and profiles, "profiles: потрібен непорожній словник")
    for key, p in profiles.items():
        where = f"profiles.{key}"
        need(isinstance(key, str) and NAME_RE.match(key), f"{where}: ключ — малі латинські літери, цифри, «-»")
        check_keys(p, PROFILE_KEYS, where)
        as_text(p.get("name"), f"{where}.name")
        need(p.get("category") in CATEGORIES, f"{where}.category: одне з {sorted(CATEGORIES)}")
        metrics = p.get("metrics")
        need(isinstance(metrics, dict) and metrics, f"{where}.metrics: потрібен непорожній словник")
        for code, m in metrics.items():
            mw = f"{where}.metrics.{code}"
            need(isinstance(code, str) and METRIC_RE.match(code), f"{mw}: код метрики — малі латинські літери, цифри, «_»")
            check_keys(m, METRIC_KEYS, mw)
            need(isinstance(m.get("oid"), str) and OID_RE.match(m["oid"]),
                 f"{mw}.oid: числовий OID рядком (напр. 1.3.6.1.2.1.1.3.0)")
            scale = m.get("scale", 1)
            need(isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale != 0,
                 f"{mw}.scale: ненульове число")

    devices = cfg["devices"]
    need(isinstance(devices, list) and devices, "devices: потрібен непорожній список")
    names, ips = set(), set()
    for i, dev in enumerate(devices):
        where = f"devices[{i}]"
        check_keys(dev, DEVICE_KEYS, where)
        need({"name", "ip", "location", "profile", "credential"} <= set(dev),
             f"{where}: обов'язкові name, ip, location, profile, credential")
        as_text(dev["name"], f"{where}.name")
        as_text(dev["location"], f"{where}.location")
        need(isinstance(dev["ip"], str), f"{where}.ip: IP-адресу треба взяти в лапки (YAML прочитав {dev['ip']!r})")
        try:
            ip = ipaddress.ip_address(dev["ip"])
        except ValueError:
            raise ConfigError(f"{where}.ip: «{dev['ip']}» не є IP-адресою вузла")
        need(not (ip.version == 6 and ip.ipv4_mapped), f"{where}.ip: IPv4-mapped IPv6 не допускається — вкажіть IPv4")
        need(dev["name"] not in names, f"{where}: name «{dev['name']}» повторюється у файлі")
        need(ip not in ips, f"{where}: ip «{ip}» повторюється у файлі")
        names.add(dev["name"]); ips.add(ip)
        dev["ip"] = str(ip)
        need(dev["profile"] in profiles, f"{where}.profile: профілю «{dev['profile']}» немає в profiles")
        need(dev["credential"] in creds, f"{where}.credential: облікового запису «{dev['credential']}» немає в credentials")
        if "port" in dev:
            as_int(dev["port"], f"{where}.port", 1, 65535)
        if "poll_interval_s" in dev:
            as_int(dev["poll_interval_s"], f"{where}.poll_interval_s", 10, 60)


def q(v):
    """Рядковий SQL-літерал (усі значення з файлу потрапляють у SQL лише так)."""
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def num(v):
    """Перевірене число як SQL-літерал NUMERIC — без жодного тексту з файлу."""
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise ConfigError(f"некоректне число {v!r}")
    need(d.is_finite(), f"некоректне число {v!r}")
    return format(d, "f")


def build_sql(cfg, raw, container_path):
    sha = hashlib.sha256(raw).hexdigest()
    d = cfg["defaults"]
    out = [
        "\\set ON_ERROR_STOP 1",
        "SET search_path TO netguard;",
        "BEGIN;",
        f"SELECT EXISTS (SELECT 1 FROM snmp_config WHERE sha256 = {q(sha)} AND is_active) AS skip,",
        f"       EXISTS (SELECT 1 FROM snmp_config WHERE sha256 = {q(sha)}) AS known \\gset",
        "\\if :skip",
        "\\echo 'Ця версія конфігурації вже активна — змін немає'",
        "ROLLBACK;",
        "\\quit",
        "\\endif",
        "UPDATE snmp_config SET is_active = FALSE WHERE is_active;",
        "\\if :known",
        "\\echo 'Повторна активація раніше застосованої версії'",
        f"UPDATE snmp_config SET is_active = TRUE, loaded_at = now(), file_path = {q(container_path)} WHERE sha256 = {q(sha)};",
        "\\else",
        "INSERT INTO snmp_config (file_path, sha256, format_version, content, is_active) VALUES "
        f"({q(container_path)}, {q(sha)}, {num(cfg['version'])}, {q(json.dumps(cfg, ensure_ascii=False))}::jsonb, TRUE);",
        "\\endif",
        "CREATE TEMP TABLE cur ON COMMIT DROP AS SELECT id FROM snmp_config WHERE is_active;",
    ]

    # credentials -> snmp_credential (UPDATE + INSERT ... WHERE NOT EXISTS: послідовність не витрачається)
    for name, c in cfg["credentials"].items():
        secret = SECRET_RE.match(c["secret"]).group(1)
        auth = c.get("auth_protocol", "SHA") if c["version"] == "v3" else None
        vals = (f"snmp_version = {q(c['version'])}, username = {q(c.get('username'))}, "
                f"auth_protocol = {q(auth)}, priv_protocol = {q(c.get('priv_protocol'))}, secret_ref = {q(secret)}")
        out.append(f"UPDATE snmp_credential SET config_id = (SELECT id FROM cur), {vals} WHERE name = {q(name)};")
        out.append(
            "INSERT INTO snmp_credential (config_id, name, snmp_version, username, auth_protocol, priv_protocol, secret_ref) "
            f"SELECT id, {q(name)}, {q(c['version'])}, {q(c.get('username'))}, {q(auth)}, {q(c.get('priv_protocol'))}, "
            f"{q(secret)} FROM cur WHERE NOT EXISTS (SELECT 1 FROM snmp_credential WHERE name = {q(name)});")

    # profiles -> equipment_type (за snmp_profile) + повна заміна шаблону snmp_oid_map
    for key, p in cfg["profiles"].items():
        vals = (f"name = {q(p['name'])}, category = {q(p['category'])}, vendor = {q(p.get('vendor'))}, "
                f"snmp_mib = {q(p.get('mib'))}")
        out.append(f"UPDATE equipment_type SET {vals} WHERE snmp_profile = {q(key)};")
        out.append(
            "INSERT INTO equipment_type (snmp_profile, name, category, vendor, snmp_mib) "
            f"SELECT {q(key)}, {q(p['name'])}, {q(p['category'])}, {q(p.get('vendor'))}, {q(p.get('mib'))} "
            f"WHERE NOT EXISTS (SELECT 1 FROM equipment_type WHERE snmp_profile = {q(key)});")
        type_id = f"(SELECT id FROM equipment_type WHERE snmp_profile = {q(key)})"
        out.append(f"DELETE FROM snmp_oid_map WHERE equipment_type_id = {type_id};")
        for code, m in p["metrics"].items():
            # невідомий код метрики -> metric_id IS NULL -> порушення NOT NULL -> відкат транзакції
            out.append(
                "INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, scale, config_id) SELECT "
                f"{type_id}, (SELECT id FROM metric WHERE code = {q(code)}), {q(m['oid'].lstrip('.'))}, "
                f"{num(m.get('scale', 1))}, id FROM cur;")

    # devices -> equipment: ідентифікація за name; IP, зайнятий іншим активним пристроєм, — помилка
    # uq_equipment_ip. Спочатку деактивуються пристрої, яких немає у файлі, — тоді їхні IP-адреси
    # можна одразу віддати новим пристроям (історія лишається за старими записами).
    names = ", ".join(q(x["name"]) for x in cfg["devices"])
    out.append(f"UPDATE equipment SET is_active = FALSE WHERE config_id IS NOT NULL AND is_active AND name NOT IN ({names});")
    for dev in cfg["devices"]:
        port = num(dev.get("port", d["port"]))
        interval = num(dev.get("poll_interval_s", d["poll_interval_s"]))
        type_id = f"(SELECT id FROM equipment_type WHERE snmp_profile = {q(dev['profile'])})"
        loc_id = f"(SELECT id FROM locations WHERE name = {q(dev['location'])})"
        cred_id = f"(SELECT id FROM snmp_credential WHERE name = {q(dev['credential'])})"
        out.append(
            f"UPDATE equipment SET type_id = {type_id}, location_id = {loc_id}, ip_address = {q(dev['ip'])}, "
            f"credential_id = {cred_id}, config_id = (SELECT id FROM cur), snmp_port = {port}, "
            f"poll_interval_s = {interval}, is_active = TRUE WHERE name = {q(dev['name'])};")
        out.append(
            "INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, config_id, snmp_port, poll_interval_s) "
            f"SELECT {type_id}, {loc_id}, {q(dev['name'])}, {q(dev['ip'])}, {cred_id}, id, {port}, {interval} FROM cur "
            f"WHERE NOT EXISTS (SELECT 1 FROM equipment WHERE name = {q(dev['name'])});")
    creds = ", ".join(q(n) for n in cfg["credentials"])
    out.append(f"DELETE FROM snmp_credential c WHERE c.name NOT IN ({creds}) "
               "AND NOT EXISTS (SELECT 1 FROM equipment e WHERE e.credential_id = c.id);")
    out.append("INSERT INTO event_log (event_type, message, details) SELECT 'config_change', "
               "'Застосовано YAML-конфігурацію SNMP', jsonb_build_object('snmp_config_id', id, 'sha256', "
               f"{q(sha)}, 'reactivated', :'known'::boolean) FROM cur;")
    out.append("COMMIT;")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    src = sys.argv[1]
    container_path = sys.argv[2] if len(sys.argv) > 2 else "/etc/netguard/snmp_config.yaml"
    try:
        raw = open(src, "rb").read()
        cfg = yaml.load(raw, Loader=StrictLoader)
        validate(cfg)
        sql = build_sql(cfg, raw, container_path)
    except (ConfigError, yaml.YAMLError) as e:
        print(f"ПОМИЛКА у {src}: {e}", file=sys.stderr)
        return 2
    print(sql)
    return 0


if __name__ == "__main__":
    sys.exit(main())

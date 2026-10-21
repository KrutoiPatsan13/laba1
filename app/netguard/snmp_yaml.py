"""Сувора перевірка YAML-конфігурації SNMP (config/snmp_config.yaml).

Спільний модуль для веб-застосунку NetGuard Monitor і командного завантажувача
tools/snmp_config_sync.py: розбирає файл (дублікати ключів заборонені), перевіряє
структуру, типи, довжини рядків і посилання між розділами. Перевірений вміст
застосовує до БД функція netguard.sp_apply_snmp_config(path, sha256, content).
"""
import hashlib
import ipaddress
import json
import re
import unicodedata
from decimal import Decimal

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
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ConfigError(f"рядок {key_node.start_mark.line + 1}: злиття ключів (<<) не підтримується — опишіть значення явно")
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


def as_text(value, where, maxlen):
    need(isinstance(value, str) and value.strip() == value and value != "",
         f"{where}: очікується непорожній рядок без пробілів на краях (за потреби — у лапках)")
    need(not any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value),
         f"{where}: рядок містить керівні або некоректні символи")
    need(len(value) <= maxlen, f"{where}: довжина {len(value)} > {maxlen} (обмеження стовпця в БД)")
    return value


def as_key(value, regex, where, maxlen, hint):
    need(isinstance(value, str) and regex.fullmatch(value), f"{where}: {hint}")
    need(len(value) <= maxlen, f"{where}: довжина {len(value)} > {maxlen} (обмеження стовпця в БД)")
    return value


def as_scale(value, where):
    """Множник для snmp_oid_map.scale NUMERIC(12,6): ненульовий, |x| < 10^6, до 6 знаків після коми."""
    need(isinstance(value, (int, float)) and not isinstance(value, bool), f"{where}: очікується число")
    d = Decimal(str(value))
    need(d.is_finite() and d != 0, f"{where}: ненульове скінченне число")
    need(abs(d) < 10 ** 6, f"{where}: |{value}| має бути менше 1000000")
    need(d.normalize().as_tuple().exponent >= -6, f"{where}: не більше 6 знаків після коми")
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
        as_key(name, NAME_RE, where, 50, "ім'я — малі латинські літери, цифри, «-»")
        check_keys(c, CREDENTIAL_KEYS, where)
        need(c.get("version") in SNMP_VERSIONS, f"{where}.version: одне з {sorted(SNMP_VERSIONS)}")
        need(isinstance(c.get("secret"), str) and SECRET_RE.fullmatch(c["secret"]),
             f"{where}.secret: лише посилання на змінну середовища ${{ІМ'Я}}, не відкритий секрет")
        need(len(c["secret"]) <= 103, f"{where}.secret: ім'я змінної довше за 100 символів")
        if c["version"] == "v3":
            # секрет у файлі обов'язковий, тож для v3 обов'язковий і протокол автентифікації
            # (рівень authNoPriv або authPriv); неявного значення за замовчуванням немає
            as_text(c.get("username"), f"{where}.username", 64)
            need(c.get("auth_protocol") in AUTH, f"{where}.auth_protocol: обов'язковий для v3, одне з {sorted(AUTH)}")
            need(c.get("priv_protocol") is None or c["priv_protocol"] in PRIV, f"{where}.priv_protocol: одне з {sorted(PRIV)}")
        else:
            need(not ({"username", "auth_protocol", "priv_protocol"} & set(c)),
                 f"{where}: username/auth_protocol/priv_protocol дозволені лише для v3")

    profiles = cfg["profiles"]
    need(isinstance(profiles, dict) and profiles, "profiles: потрібен непорожній словник")
    pnames = set()
    for key, p in profiles.items():
        where = f"profiles.{key}"
        as_key(key, NAME_RE, where, 50, "ключ — малі латинські літери, цифри, «-»")
        check_keys(p, PROFILE_KEYS, where)
        as_text(p.get("name"), f"{where}.name", 100)
        need(not p["name"].startswith("~"), f"{where}.name: не може починатися з «~» (службовий префікс)")
        need(p["name"] not in pnames, f"{where}.name: назва «{p['name']}» повторюється у файлі")
        pnames.add(p["name"])
        need(p.get("category") in CATEGORIES, f"{where}.category: одне з {sorted(CATEGORIES)}")
        if "vendor" in p:
            as_text(p["vendor"], f"{where}.vendor", 50)
        if "mib" in p:
            as_text(p["mib"], f"{where}.mib", 100)
        metrics = p.get("metrics")
        need(isinstance(metrics, dict) and metrics, f"{where}.metrics: потрібен непорожній словник")
        for code, m in metrics.items():
            mw = f"{where}.metrics.{code}"
            as_key(code, METRIC_RE, mw, 50, "код метрики — малі латинські літери, цифри, «_»")
            check_keys(m, METRIC_KEYS, mw)
            need(isinstance(m.get("oid"), str) and OID_RE.fullmatch(m["oid"]),
                 f"{mw}.oid: числовий OID рядком (напр. 1.3.6.1.2.1.1.3.0)")
            need(len(m["oid"].lstrip(".")) <= 128, f"{mw}.oid: довжина > 128 (обмеження стовпця в БД)")
            as_scale(m.get("scale", 1), f"{mw}.scale")

    devices = cfg["devices"]
    need(isinstance(devices, list) and devices, "devices: потрібен непорожній список")
    names, ips = set(), set()
    for i, dev in enumerate(devices):
        where = f"devices[{i}]"
        check_keys(dev, DEVICE_KEYS, where)
        need({"name", "ip", "location", "profile", "credential"} <= set(dev),
             f"{where}: обов'язкові name, ip, location, profile, credential")
        as_text(dev["name"], f"{where}.name", 100)
        as_text(dev["location"], f"{where}.location", 100)
        as_text(dev["profile"], f"{where}.profile", 50)
        as_text(dev["credential"], f"{where}.credential", 50)
        need(isinstance(dev["ip"], str), f"{where}.ip: IP-адресу треба взяти в лапки (YAML прочитав {dev['ip']!r})")
        need("%" not in dev["ip"], f"{where}.ip: ідентифікатор зони (%…) у БД не зберігається — вкажіть адресу без нього")
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


def load(raw: bytes) -> tuple[dict, str]:
    """Розбирає й перевіряє вміст файлу; повертає (конфігурація, SHA-256 сирих байтів).

    Будь-яка помилка у файлі — ConfigError з повідомленням українською.
    """
    try:
        cfg = yaml.load(raw, Loader=StrictLoader)
        validate(cfg)
    except ConfigError:
        raise
    except yaml.YAMLError as e:
        raise ConfigError(f"некоректний YAML: {e}") from None
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as e:
        raise ConfigError(f"значення неочікуваного типу або формату ({type(e).__name__}: {e})") from None
    try:
        to_json(cfg).encode("utf-8")
    except UnicodeError:
        raise ConfigError("файл містить некоректні символи Unicode") from None
    return cfg, hashlib.sha256(raw).hexdigest()


def to_json(cfg: dict) -> str:
    return json.dumps(cfg, ensure_ascii=False)

"""Налаштування NetGuard Monitor — зі змінних середовища (12-factor).

NETGUARD_DSN            рядок підключення до PostgreSQL (libpq), напр.
                        "host=localhost dbname=netguard user=netguard password=..."
                        (потрібен сервісам із доступом до БД; Web UI до БД не підключається)
NETGUARD_JWT_SECRET     секрет підпису токенів доступу (JWT); спільний для API Gateway і Reporting Service
NETGUARD_JWT_TTL_MIN    строк дії токена, хв (типово 480)
NETGUARD_STORAGE_SECRET секрет для підпису сесій веб-інтерфейсу
NETGUARD_WEB_PORT       порт веб-інтерфейсу (типово 8080)
NETGUARD_GATEWAY_PORT   порт API Gateway / Auth Service (типово 8081)
NETGUARD_REPORTING_PORT порт Reporting Service (типово 8082)
NETGUARD_GATEWAY_URL    адреса API Gateway для веб-інтерфейсу (типово http://localhost:8081)
NETGUARD_REPORTING_URL  адреса Reporting Service для API Gateway (типово http://localhost:8082)
NETGUARD_BIND_HOST      інтерфейс, на якому слухають HTTP-сервіси (типово 127.0.0.1; у контейнері — 0.0.0.0)
NETGUARD_SIM_FAULTS     несправності для емулятора SNMP-агентів, напр.
                        "SW-LUK-02=offline;UPS-TER-01=battery_low"
NETGUARD_SNMP_CONFIG    шлях до YAML-файлу конфігурації SNMP; Polling Service застосовує його
                        під час старту та після кожної зміни (порожньо — лише вебінтерфейс і CLI)
NETGUARD_NOTIFY_DRY_RUN 1 — e-mail і Telegram не надсилаються, а пишуться у var/outbox.log
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM — канал e-mail
TELEGRAM_BOT_TOKEN      канал Telegram
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent.parent
VAR_DIR = Path(os.environ.get("NETGUARD_VAR_DIR", REPO_DIR / "var"))


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    dsn: str = os.environ.get("NETGUARD_DSN", "dbname=netguard")
    jwt_secret: str = os.environ.get("NETGUARD_JWT_SECRET", "change-me-in-production-jwt")
    jwt_ttl_min: int = int(os.environ.get("NETGUARD_JWT_TTL_MIN", "480"))
    storage_secret: str = os.environ.get("NETGUARD_STORAGE_SECRET", "change-me-in-production")
    bind_host: str = os.environ.get("NETGUARD_BIND_HOST", "127.0.0.1")
    web_port: int = int(os.environ.get("NETGUARD_WEB_PORT", "8080"))
    gateway_port: int = int(os.environ.get("NETGUARD_GATEWAY_PORT", "8081"))
    reporting_port: int = int(os.environ.get("NETGUARD_REPORTING_PORT", "8082"))
    gateway_url: str = os.environ.get("NETGUARD_GATEWAY_URL", "http://localhost:8081").rstrip("/")
    reporting_url: str = os.environ.get("NETGUARD_REPORTING_URL", "http://localhost:8082").rstrip("/")
    sim_faults: str = os.environ.get("NETGUARD_SIM_FAULTS", "")
    snmp_config: Path | None = Path(os.environ["NETGUARD_SNMP_CONFIG"]) if os.environ.get("NETGUARD_SNMP_CONFIG") else None
    notify_dry_run: bool = _bool("NETGUARD_NOTIFY_DRY_RUN", True)
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "netguard@localhost")
    telegram_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    escalate_after_min: int = int(os.environ.get("NETGUARD_ESCALATE_AFTER_MIN", "5"))
    var_dir: Path = field(default=VAR_DIR)


settings = Settings()

"""Налаштування NetGuard Monitor — зі змінних середовища (12-factor).

NETGUARD_DSN            рядок підключення до PostgreSQL (libpq), напр.
                        "host=localhost dbname=netguard user=netguard password=..."
NETGUARD_STORAGE_SECRET секрет для підпису сесій веб-інтерфейсу
NETGUARD_WEB_PORT       порт веб-інтерфейсу (типово 8080)
NETGUARD_SIM_FAULTS     несправності для емулятора SNMP-агентів, напр.
                        "SW-VYN-02=offline;UPS-TRO-01=battery_low"
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
    storage_secret: str = os.environ.get("NETGUARD_STORAGE_SECRET", "change-me-in-production")
    web_port: int = int(os.environ.get("NETGUARD_WEB_PORT", "8080"))
    sim_faults: str = os.environ.get("NETGUARD_SIM_FAULTS", "")
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

"""Емулятор SNMP-агентів обладнання для демонстрації та тестування без реальних пристроїв.

Повертає «сирі» значення OID так, як їх віддав би агент (TimeTicks для uptime, десяті
частки градуса для MikroTik тощо): Polling Service множить їх на scale із шаблону
snmp_oid_map, як і для справжнього SNMP. Значення мають добовий цикл і шум;
несправності задаються змінною NETGUARD_SIM_FAULTS, напр.
"SW-VYN-02=offline;UPS-TRO-01=battery_low".

Ті самі формули використовує скрипт demo_setup для генерування історії показників,
тож графіки історії та живі дані не мають розриву.
"""
import math
import random
from datetime import datetime, timedelta, timezone

KYIV = timezone(timedelta(hours=3))          # для добового циклу достатньо фіксованого зсуву
UPTIME_ANCHOR = datetime(2026, 9, 1, tzinfo=timezone.utc)
FAULTS = {"offline", "battery_low", "overheat", "cpu_high", "power_outage"}


class SnmpTimeout(Exception):
    """Агент не відповів у межах timeout × retries."""


def parse_faults(spec: str) -> dict[str, str]:
    faults = {}
    for part in filter(None, (p.strip() for p in spec.split(";"))):
        name, _, kind = part.partition("=")
        if kind.strip() in FAULTS:
            faults[name.strip()] = kind.strip()
    return faults


def _daily(ts: datetime) -> float:
    """Добовий цикл навантаження: −1 о 03:00, +1 о 15:00 за київським часом."""
    local = ts.astimezone(KYIV)
    h = local.hour + local.minute / 60
    return math.sin(2 * math.pi * (h - 9) / 24)


def baseline(code: str, category: str, dev_id: int, ts: datetime, rnd: random.Random,
             boot: datetime | None = None) -> float:
    """Нормальне (без несправностей) значення метрики в одиницях БД."""
    d = _daily(ts)
    if code == "battery_charge":
        return round(100 - abs(rnd.gauss(0, 0.2)), 1)
    if code == "battery_temperature":
        return round(24 + 1.5 * d + (dev_id % 3) * 0.7 + rnd.gauss(0, 0.25), 1)
    if code == "output_load":
        return round(40 + 12 * d + (dev_id % 5) + rnd.gauss(0, 1.2), 1)
    if code == "battery_runtime":
        load = 40 + 12 * d + (dev_id % 5)
        return round(5400 - 45 * load + rnd.gauss(0, 30))
    if code == "battery_voltage":
        return round(218 + rnd.gauss(0, 0.5), 1)
    if code == "input_voltage":
        return round(229 + 4 * math.sin(4 * math.pi * ts.timestamp() / 86400) + rnd.gauss(0, 0.8), 1)
    if code == "output_voltage":
        return round(230 + rnd.gauss(0, 0.3), 1)
    if code == "uptime":
        boot = boot or UPTIME_ANCHOR + timedelta(hours=7 * dev_id)
        return max(0, round((ts - boot).total_seconds()))
    if code == "cpu_load":
        if category == "router":
            return round(min(100, max(0, 25 + 15 * d + rnd.gauss(0, 3))), 1)
        return round(min(100, max(0, 12 + 8 * d + rnd.gauss(0, 2.5))), 1)
    if code == "temperature":
        if category == "router":
            return round(48 + 4 * d + rnd.gauss(0, 0.6), 1)
        return round(40 + 3 * d + (dev_id % 4) + rnd.gauss(0, 0.5), 1)
    return 0.0


class Emulator:
    """Набір емульованих SNMP-агентів (по одному на пристрій)."""

    def __init__(self, faults: dict[str, str] | None = None, boot_times: dict[int, datetime] | None = None):
        self.faults = faults or {}
        self.boot_times = boot_times or {}
        self.rnd = random.Random()

    def get(self, device: dict, oids: list[dict], ts: datetime) -> dict[str, float]:
        """SNMP GET: oids — рядки плану опитування (oid, metric_code, scale); повертає {oid: сире значення}."""
        fault = self.faults.get(device["equipment_name"])
        if fault == "offline":
            raise SnmpTimeout(f"{device['ip']}:{device['snmp_port']} не відповідає")
        result = {}
        for o in oids:
            value = baseline(o["metric_code"], device["category"], device["equipment_id"], ts, self.rnd,
                             self.boot_times.get(device["equipment_id"]))
            value = self._apply_fault(fault, o["metric_code"], value)
            scale = float(o["scale"])
            result[o["oid"]] = round(value / scale) if o["metric_code"] == "uptime" else value / scale
        return result

    def _apply_fault(self, fault: str | None, code: str, value: float) -> float:
        if fault == "battery_low" and code == "battery_charge":
            return round(46 + self.rnd.gauss(0, 0.3), 1)
        if fault == "battery_low" and code == "battery_runtime":
            return round(value * 0.46)
        if fault == "power_outage" and code == "input_voltage":
            return 0.0
        if fault == "overheat" and code in ("temperature", "battery_temperature"):
            return round(value + 22, 1)
        if fault == "cpu_high" and code == "cpu_load":
            return round(96 + self.rnd.gauss(0, 0.8), 1)
        return value

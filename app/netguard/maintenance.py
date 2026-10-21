"""Обслуговування БД: секції телеметрії та резервні копії (NFR-02).

Запускається планувальником (cron, systemd timer) від імені власника схеми netguard —
створення й видалення секцій є DDL, тому ролі сервісів ng_* цього не можуть.

    python -m netguard.maintenance partitions [--ahead 3] [--keep 6]
    python -m netguard.maintenance backup [--dir var/backup] [--keep-files 14]
    python -m netguard.maintenance restore <файл.dump>

partitions — створює місячні секції metric_reading на --ahead місяців уперед
             (fn_create_partitions) і видаляє секції, старші за --keep місяців
             (fn_drop_old_partitions). 500 пристроїв × 5 метрик щохвилини — ≈ 10,5 ГБ
             на місяць, тож 6 місяців історії вміщуються на диск 100 ГБ.
backup     — повна копія БД у форматі pg_dump custom (стиснена; відновлюється
             вибірково); у каталозі лишаються --keep-files останніх копій.
restore    — відновлює копію в БД з NETGUARD_DSN (pg_restore --clean --if-exists).
             Ролі ng_* — об'єкти кластера, у копію БД не входять: на новому сервері
             спершу виконайте sql/lr2_logic.sql або pg_dumpall --roles-only.
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .db import connect
from .settings import settings


def partitions(ahead: int, keep: int):
    with connect(autocommit=True) as conn:
        created = conn.execute("SELECT fn_create_partitions(%s) AS n", (ahead,)).fetchone()["n"]
        dropped = [r["name"] for r in conn.execute("SELECT fn_drop_old_partitions(%s) AS name", (keep,))]
        conn.execute("SELECT fn_log_event('user_action', 'Обслуговування секцій телеметрії', %s::jsonb)",
                     (f'{{"створено": {created}, "видалено": {len(dropped)}}}',))
    print(f"створено секцій: {created}; видалено: {', '.join(dropped) or 'немає'}")


def backup(directory: Path, keep_files: int):
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"netguard_{datetime.now():%Y%m%d_%H%M%S}.dump"
    subprocess.run(["pg_dump", "--format=custom", "--file", str(out), "--dbname", settings.dsn], check=True)
    for old in sorted(directory.glob("netguard_*.dump"))[:-keep_files]:
        old.unlink()
    print(f"резервну копію записано: {out} ({out.stat().st_size / 2**20:.1f} МБ)")


def restore(file: Path):
    if not file.is_file():
        sys.exit(f"файл {file} не знайдено")
    subprocess.run(["pg_restore", "--clean", "--if-exists", "--exit-on-error", "--single-transaction",
                    "--dbname", settings.dsn, str(file)], check=True)
    print(f"БД відновлено з {file}")


def main():
    ap = argparse.ArgumentParser(prog="python -m netguard.maintenance", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("partitions", help="створити майбутні й видалити застарілі секції телеметрії")
    p.add_argument("--ahead", type=int, default=3)
    p.add_argument("--keep", type=int, default=6)
    b = sub.add_parser("backup", help="резервна копія БД (pg_dump)")
    b.add_argument("--dir", type=Path, default=settings.var_dir / "backup")
    b.add_argument("--keep-files", type=int, default=14)
    r = sub.add_parser("restore", help="відновлення БД з копії (pg_restore)")
    r.add_argument("file", type=Path)
    a = ap.parse_args()
    if a.cmd == "partitions":
        if a.ahead < 1 or a.keep < 1:
            sys.exit("--ahead і --keep мають бути додатними")
        partitions(a.ahead, a.keep)
    elif a.cmd == "backup":
        if a.keep_files < 1:
            sys.exit("--keep-files має бути додатним")
        backup(a.dir, a.keep_files)
    else:
        restore(a.file)


if __name__ == "__main__":
    main()

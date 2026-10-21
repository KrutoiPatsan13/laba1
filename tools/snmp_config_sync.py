"""Застосування YAML-конфігурації SNMP до БД NetGuard Monitor з командного рядка.

Читає config/snmp_config.yaml, суворо перевіряє його (модуль netguard.snmp_yaml) і друкує
SQL-скрипт для psql, який в одній транзакції викликає функцію БД
netguard.sp_apply_snmp_config(path, sha256, content). Та сама функція застосовує файл
і з веб-інтерфейсу, тож логіка синхронізації одна:

* файл, чия контрольна сума збігається з активною версією, — пропуск, нічого не змінюється;
* файл, що вже застосовувався раніше (та сама контрольна сума, версія неактивна), —
  ця версія знову стає активною, і таблиці синхронізуються з її вмістом;
* профіль ідентифікується за ключем (equipment_type.snmp_profile); профілі, яких більше немає
  у файлі, лишаються в БД (на них посилаються пороги та історія), назви профілів можна
  поміняти місцями;
* пристрій ідентифікується за name: зміна IP оновлює наявний запис (історія зберігається),
  пристрої у файлі можуть обмінятися IP-адресами, а IP, зайнятий активним пристроєм поза
  файлом, — помилка й відкат;
* пристрої, яких більше немає у файлі, деактивуються (is_active = FALSE), не видаляються;
  їхні IP-адреси можна віддати новим пристроям (перейменування = новий запис);
* облікові записи, яких немає у файлі, видаляються лише тоді, коли на них не посилається
  жоден пристрій;
* будь-яка помилка в БД відкочує транзакцію — активною лишається попередня версія.

usage:
    set -o pipefail
    python3 tools/snmp_config_sync.py config/snmp_config.yaml | psql -d netguard -v ON_ERROR_STOP=1

Помилка у файлі — повідомлення в stderr і код виходу 2 (SQL не друкується).
Потрібні об'єкти БД зі скриптів sql/schema.sql і sql/lr2_logic.sql.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from netguard.snmp_yaml import ConfigError, load, to_json  # noqa: E402


def q(v):
    """Рядковий SQL-літерал (усі значення з файлу потрапляють у SQL лише так)."""
    return "'" + str(v).replace("'", "''") + "'"


def build_sql(cfg, sha, container_path):
    return "\n".join([
        "\\set ON_ERROR_STOP 1",
        "SET search_path TO netguard, public;",
        "BEGIN;",
        f"SELECT sp_apply_snmp_config({q(container_path)}, {q(sha)}, {q(to_json(cfg))}::jsonb) AS result \\gset",
        "SELECT CASE :'result' WHEN 'skipped' THEN 'Ця версія конфігурації вже активна — змін немає'"
        " WHEN 'reactivated' THEN 'Повторно активовано раніше застосовану версію'"
        " ELSE 'Застосовано нову версію конфігурації' END AS \"Результат\";",
        "COMMIT;",
    ])


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    src = sys.argv[1]
    container_path = sys.argv[2] if len(sys.argv) > 2 else "/etc/netguard/snmp_config.yaml"
    try:
        raw = open(src, "rb").read()
    except OSError as e:
        print(f"ПОМИЛКА: не вдалося прочитати {src}: {e}", file=sys.stderr)
        return 2
    try:
        cfg, sha = load(raw)
    except ConfigError as e:
        print(f"ПОМИЛКА у {src}: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(build_sql(cfg, sha, container_path) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

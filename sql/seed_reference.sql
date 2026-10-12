-- =====================================================================
--  NetGuard Monitor — довідкові дані, потрібні до застосування
--  config/snmp_config.yaml (YAML посилається на коди метрик і назви вузлів)
--
--  Запуск (після sql/schema.sql):
--    psql -d netguard -v ON_ERROR_STOP=1 -f sql/seed_reference.sql
--  Скрипт можна виконувати повторно: наявні рядки не змінюються.
-- =====================================================================

SET search_path TO netguard;

BEGIN;

INSERT INTO roles (code, name, description) VALUES
    ('support', 'Сапорт',        'Інженер технічної підтримки: стан обладнання, інциденти, звіти'),
    ('admin',   'Адміністратор', 'Повний доступ: обладнання, пороги, користувачі, конфігурація')
ON CONFLICT (code) DO NOTHING;

INSERT INTO locations (name, location_type, address, description) VALUES
    ('ЦОД',           'data_center', 'вул. Центральна, 1',      'Головний вузол провайдера'),
    ('Вузол Оболонь', 'access_node', 'просп. Оболонський, 10',  'Районний вузол доступу')
ON CONFLICT (name) DO NOTHING;

-- Коди метрик збігаються з ключами profiles.*.metrics у YAML-файлі
INSERT INTO metric (code, name, unit, description) VALUES
    ('battery_charge',      'Заряд АКБ',                     '%',  'PowerNet-MIB upsAdvBatteryCapacity'),
    ('battery_temperature', 'Температура АКБ',               '°C', 'PowerNet-MIB upsAdvBatteryTemperature'),
    ('battery_runtime',     'Залишковий час роботи від АКБ', 'с',  'PowerNet-MIB upsAdvBatteryRunTimeRemaining'),
    ('battery_voltage',     'Напруга АКБ',                   'В',  'PowerNet-MIB upsAdvBatteryActualVoltage'),
    ('input_voltage',       'Вхідна напруга',                'В',  'PowerNet-MIB upsAdvInputLineVoltage'),
    ('output_voltage',      'Вихідна напруга',               'В',  'PowerNet-MIB upsAdvOutputVoltage'),
    ('output_load',         'Навантаження ДБЖ',              '%',  'PowerNet-MIB upsAdvOutputLoad'),
    ('uptime',              'Час безвідмовної роботи',       'с',  'SNMPv2-MIB sysUpTime'),
    ('cpu_load',            'Завантаження CPU',              '%',  'CISCO-PROCESS-MIB cpmCPUTotal5minRev'),
    ('temperature',         'Температура пристрою',          '°C', 'CISCO-ENVMON-MIB ciscoEnvMonTemperatureStatusValue')
ON CONFLICT (code) DO NOTHING;

COMMIT;

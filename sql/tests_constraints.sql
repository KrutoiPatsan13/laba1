-- =====================================================================
--  NetGuard Monitor — перевірка обмежень цілісності фізичної моделі
--
--  Запуск (після sql/schema.sql; довідкові дані не обов'язкові):
--    psql -d netguard -v ON_ERROR_STOP=1 -f sql/tests_constraints.sql
--
--  1) Позитивна частина: вставляє узгоджений набір даних і проводить
--     інцидент усім життєвим циклом — кожна інструкція має виконатися.
--  2) Негативна частина: 51 некоректна операція. Кожна має бути відхилена
--     саме тим обмеженням, для якого її складено (PASS), інакше — FAIL.
--  Усе виконується в одній транзакції, яка наприкінці відкочується:
--  тест не залишає даних у БД. Якщо хоч одна перевірка FAIL — psql
--  завершується з помилкою.
-- =====================================================================

\set QUIET on
SET search_path TO netguard;
BEGIN;
-- перевіряються декларативні обмеження схеми, тому програмна логіка sql/lr2_logic.sql
-- (автоматичні інциденти, перевірки життєвого циклу) на час тесту вимикається;
-- без lr2_logic.sql цей параметр ні на що не впливає
SELECT set_config('netguard.bypass_checks', 'on', true) \gset bypass_

CREATE TEMP TABLE test_result (
    no       INTEGER PRIMARY KEY,
    check_   TEXT,
    expected TEXT,
    actual   TEXT,
    result   TEXT
) ON COMMIT DROP;

-- Виконує p_sql і перевіряє, що його відхилено обмеженням, ім'я якого
-- відповідає регулярному виразу p_expected
CREATE FUNCTION pg_temp.expect_error(p_no INTEGER, p_check TEXT, p_sql TEXT, p_expected TEXT)
RETURNS VOID LANGUAGE plpgsql AS $fn$
DECLARE
    v_constraint TEXT;
    v_message    TEXT;
BEGIN
    BEGIN
        EXECUTE p_sql;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME, v_message = MESSAGE_TEXT;
        INSERT INTO test_result VALUES (p_no, p_check, p_expected,
            coalesce(nullif(v_constraint, ''), v_message),
            CASE WHEN coalesce(v_constraint, '') ~ p_expected THEN 'PASS' ELSE 'FAIL' END);
        RETURN;
    END;
    INSERT INTO test_result VALUES (p_no, p_check, p_expected, '(операцію виконано без помилки)', 'FAIL');
END $fn$;

-- ---------------------------------------------------------------------
-- 1. Позитивна частина: коректні дані (префікс «t» — тестові записи)
-- ---------------------------------------------------------------------
INSERT INTO roles (code, name) VALUES ('t_support', 'Тест: сапорт'), ('t_admin', 'Тест: адміністратор');
INSERT INTO users (role_id, full_name, login, password_hash, email, telegram_chat_id)
SELECT r.id, u.full_name, u.login, '$2b$12$abcdefghijklmnopqrstuuMZ6lkxwz0PQ0J3x1cUuQb5o3k7JdGbS', u.email, u.chat
FROM (VALUES ('t_admin',   'Тест Адміністратор', 't.admin',    't.admin@isp.example',    9000000001),
             ('t_support', 'Тест Інженер',       't.engineer', 't.engineer@isp.example', NULL)
     ) AS u(role_code, full_name, login, email, chat)
JOIN roles r ON r.code = u.role_code;

INSERT INTO locations (name, location_type, address) VALUES
    ('Т: ЦОД', 'data_center', 'тестова адреса, 1'), ('Т: вузол', 'access_node', 'тестова адреса, 2');
INSERT INTO equipment_type (name, category, vendor, snmp_mib, snmp_profile) VALUES
    ('Т: ДБЖ APC Symmetra PX', 'ups', 'APC', 'PowerNet-MIB', 't-apc'),
    ('Т: комутатор', 'switch', 'Cisco', 'CISCO-ENVMON-MIB', 't-switch');
INSERT INTO metric (code, name, unit) VALUES ('t_charge', 'Тест: заряд АКБ', '%'), ('t_temp', 'Тест: температура', '°C');

INSERT INTO snmp_config (file_path, sha256, format_version, content)
VALUES ('/etc/netguard/snmp_config.yaml', repeat('a', 64), 1, '{"version": 1}');

SELECT (SELECT id FROM roles WHERE code = 't_support')            AS role_support,
       (SELECT id FROM users WHERE login = 't.engineer')          AS user_eng,
       (SELECT id FROM locations WHERE name = 'Т: ЦОД')           AS loc_dc,
       (SELECT id FROM locations WHERE name = 'Т: вузол')         AS loc_node,
       (SELECT id FROM equipment_type WHERE snmp_profile = 't-apc')    AS type_ups,
       (SELECT id FROM equipment_type WHERE snmp_profile = 't-switch') AS type_sw,
       (SELECT id FROM metric WHERE code = 't_charge')            AS m_charge,
       (SELECT id FROM metric WHERE code = 't_temp')              AS m_temp,
       (SELECT id FROM snmp_config WHERE sha256 = repeat('a', 64)) AS cfg
\gset

INSERT INTO snmp_credential (config_id, name, snmp_version, username, auth_protocol, priv_protocol, secret_ref) VALUES
    (:cfg, 't-ups-v3',  'v3',  'netguard-poller', 'SHA256', 'AES', 'SNMP_T_UPS_SECRET'),
    (:cfg, 't-net-v2c', 'v2c', NULL,              NULL,     NULL,  'SNMP_T_NET_COMMUNITY');
INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, scale, config_id) VALUES
    (:type_ups, :m_charge, '1.3.6.1.4.1.318.1.1.1.2.2.1.0', 1, :cfg),
    (:type_sw,  :m_temp,   '1.3.6.1.4.1.9.9.13.1.3.1.3.1',  1, :cfg);

SELECT (SELECT id FROM snmp_credential WHERE name = 't-ups-v3')  AS cred_ups,
       (SELECT id FROM snmp_credential WHERE name = 't-net-v2c') AS cred_net
\gset

INSERT INTO equipment (type_id, location_id, name, model, serial_number, ip_address, credential_id, config_id) VALUES
    (:type_ups, :loc_dc,   't-ups',    'Symmetra PX 40kW', 'T-SN-001', '192.0.2.10', :cred_ups, :cfg),
    (:type_sw,  :loc_node, 't-switch', 'Catalyst 2960',    'T-SN-002', '192.0.2.2',  :cred_net, :cfg);
SELECT (SELECT id FROM equipment WHERE name = 't-ups')    AS eq_ups,
       (SELECT id FROM equipment WHERE name = 't-switch') AS eq_sw
\gset

INSERT INTO ups_details (equipment_id, rated_power_va, rated_power_w, battery_capacity_ah, battery_count)
VALUES (:eq_ups, 40000, 40000, 9.0, 40);

INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value, max_value) VALUES
    (:type_ups, :m_charge, 'critical', 30, NULL),
    (:type_ups, :m_temp,   'warning',  NULL, 40);
SELECT id AS thr_charge FROM threshold WHERE equipment_type_id = :type_ups AND metric_id = :m_charge \gset

-- показники потрапляють у секції за місяцем; дата поза діапазоном — у секцію default
INSERT INTO metric_reading (equipment_id, metric_id, read_at, value) VALUES
    (:eq_ups, :m_charge, date_trunc('minute', now()), 25.0),
    (:eq_ups, :m_temp,   date_trunc('minute', now()), 35.5),
    (:eq_ups, :m_charge, '2020-01-01 00:00+00',       90.0),
    (:eq_sw,  :m_temp,   date_trunc('minute', now()) + INTERVAL '1 month', 50.0);

-- інцидент порушення порогу проходить увесь життєвий цикл
INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value) VALUES (:eq_ups, 'threshold', :thr_charge, 25.0);
INSERT INTO incident (equipment_id, incident_type) VALUES (:eq_sw, 'offline');
SELECT (SELECT id FROM incident WHERE equipment_id = :eq_ups) AS inc_ups,
       (SELECT id FROM incident WHERE equipment_id = :eq_sw)  AS inc_sw
\gset
INSERT INTO notification (incident_id, user_id, channel) VALUES (:inc_ups, :user_eng, 'telegram'), (:inc_ups, :user_eng, 'web');
UPDATE incident SET status = 'acknowledged', acknowledged_at = now(), acknowledged_by = :user_eng WHERE id = :inc_ups;
UPDATE incident SET status = 'resolved', resolved_at = now() WHERE id = :inc_ups;
UPDATE incident SET status = 'closed', closed_at = now(), close_comment = 'Замінено батарейний модуль' WHERE id = :inc_ups;
INSERT INTO event_log (event_type, user_id, incident_id, message, details)
VALUES ('incident', :user_eng, :inc_ups, 'Інцидент закрито', '{"from": "resolved", "to": "closed"}');

-- обладнання без інцидентів видаляється разом з телеметрією (CASCADE)
INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (:type_sw, :loc_node, 't-tmp', '192.0.2.99', :cred_net);
INSERT INTO metric_reading SELECT id, :m_temp, now(), 1 FROM equipment WHERE name = 't-tmp';
DELETE FROM equipment WHERE name = 't-tmp';

-- IP виведеного з експлуатації пристрою можна віддати новому (унікальність лише серед активних)
INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, is_active)
VALUES (:type_sw, :loc_node, 't-old', '192.0.2.50', :cred_net, FALSE);
INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (:type_sw, :loc_node, 't-new', '192.0.2.50', :cred_net);

-- (усередині DO psql-змінні не підставляються, тому — підзапити за назвами)
DO $chk$
BEGIN
    ASSERT (SELECT i.status FROM incident i JOIN equipment e ON e.id = i.equipment_id WHERE e.name = 't-ups') = 'closed',
           'інцидент t-ups має бути закритим';
    ASSERT (SELECT count(*) FROM metric_reading r JOIN equipment e ON e.id = r.equipment_id
            WHERE e.name IN ('t-ups', 't-switch')) = 4, 'має бути 4 тестові показники';
    ASSERT NOT EXISTS (SELECT 1 FROM equipment WHERE name = 't-tmp'), 't-tmp має бути видалено';
    ASSERT (SELECT count(*) FROM equipment WHERE ip_address = '192.0.2.50') = 2, 'IP неактивного пристрою має бути доступним';
END $chk$;

\echo '--- Розподіл тестових показників за секціями ---'
SELECT tableoid::regclass AS partition, count(*) AS rows
FROM metric_reading WHERE equipment_id IN (:eq_ups, :eq_sw) GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------------
-- 2. Негативна частина: 51 некоректна операція
-- ---------------------------------------------------------------------
-- користувачі
SELECT pg_temp.expect_error(1, 'Логін із пробілом і великими літерами',
    format($q$INSERT INTO users (role_id, full_name, login, password_hash) VALUES (%s, 'X', 'Bad Login', 'h')$q$, :role_support),
    '^ck_users_login$') \gset
SELECT pg_temp.expect_error(2, 'Некоректний e-mail',
    format($q$INSERT INTO users (role_id, full_name, login, password_hash, email) VALUES (%s, 'X', 't.okuser', 'h', 'not-an-email')$q$, :role_support),
    '^ck_users_email$') \gset
-- обладнання
SELECT pg_temp.expect_error(3, 'Повторна IP-адреса',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (%s, %s, 't-dup', '192.0.2.10', %s)$q$, :type_sw, :loc_dc, :cred_net),
    '^uq_equipment_ip$') \gset
SELECT pg_temp.expect_error(4, 'Адреса мережі замість адреси вузла',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (%s, %s, 't-net', '192.0.2.0/24', %s)$q$, :type_sw, :loc_dc, :cred_net),
    '^ck_equipment_ip_host$') \gset
SELECT pg_temp.expect_error(5, 'Порт SNMP поза 1–65535',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, snmp_port) VALUES (%s, %s, 't-port', '192.0.2.98', %s, 70000)$q$, :type_sw, :loc_dc, :cred_net),
    '^ck_equipment_port$') \gset
SELECT pg_temp.expect_error(6, 'Інтервал опитування 300 с (> 60)',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, poll_interval_s) VALUES (%s, %s, 't-slow', '192.0.2.97', %s, 300)$q$, :type_sw, :loc_dc, :cred_net),
    '^ck_equipment_interval$') \gset
SELECT pg_temp.expect_error(7, 'Активна потужність ДБЖ більша за повну',
    format($q$INSERT INTO ups_details (equipment_id, rated_power_va, rated_power_w, battery_capacity_ah) VALUES (%s, 1000, 1500, 7)$q$, :eq_sw),
    '^ck_ups_power_w$') \gset
-- пороги
SELECT pg_temp.expect_error(8, 'Поріг без жодної межі',
    format($q$INSERT INTO threshold (equipment_type_id, metric_id, min_value, max_value) VALUES (%s, %s, NULL, NULL)$q$, :type_sw, :m_temp),
    '^ck_threshold_bounds$') \gset
SELECT pg_temp.expect_error(9, 'Нижня межа більша за верхню',
    format($q$INSERT INTO threshold (equipment_type_id, metric_id, min_value, max_value) VALUES (%s, %s, 50, 10)$q$, :type_sw, :m_temp),
    '^ck_threshold_range$') \gset
SELECT pg_temp.expect_error(10, 'Другий поріг того самого рівня для пари «тип — метрика»',
    format($q$INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value) VALUES (%s, %s, 'critical', 20)$q$, :type_ups, :m_charge),
    '^uq_threshold$') \gset
-- телеметрія
SELECT pg_temp.expect_error(11, 'Повторний показник на той самий момент',
    format($q$INSERT INTO metric_reading VALUES (%s, %s, date_trunc('minute', now()), 1)$q$, :eq_ups, :m_charge),
    '^metric_reading_.*pkey$') \gset
SELECT pg_temp.expect_error(12, 'Значення NaN',
    format($q$INSERT INTO metric_reading VALUES (%s, %s, now() - INTERVAL '1 second', 'NaN')$q$, :eq_ups, :m_charge),
    '^ck_reading_value_finite$') \gset
SELECT pg_temp.expect_error(13, 'Показник неіснуючого пристрою',
    format($q$INSERT INTO metric_reading VALUES (2147483000, %s, now(), 1)$q$, :m_charge),
    '^fk_reading_equipment$') \gset
-- інциденти
SELECT pg_temp.expect_error(14, 'Другий відкритий інцидент «офлайн» того самого пристрою',
    format($q$INSERT INTO incident (equipment_id, incident_type) VALUES (%s, 'offline')$q$, :eq_sw),
    '^uq_incident_open$') \gset
SELECT pg_temp.expect_error(15, 'Інцидент «офлайн» із порогом',
    format($q$INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value) VALUES (%s, 'offline', %s, 1)$q$, :eq_ups, :thr_charge),
    '^ck_incident_threshold$') \gset
SELECT pg_temp.expect_error(16, 'Інцидент «поріг» без порогу',
    format($q$INSERT INTO incident (equipment_id, incident_type) VALUES (%s, 'threshold')$q$, :eq_ups),
    '^ck_incident_threshold$') \gset
SELECT pg_temp.expect_error(17, 'Прийнятий інцидент без відповідального',
    format($q$UPDATE incident SET status = 'acknowledged' WHERE id = %s$q$, :inc_sw),
    '^ck_incident_ack$') \gset
SELECT pg_temp.expect_error(18, 'Закриття з порожнім коментарем',
    format($q$UPDATE incident SET status = 'closed', acknowledged_at = now(), acknowledged_by = %s, resolved_at = now(), closed_at = now(), close_comment = '   ' WHERE id = %s$q$, :user_eng, :inc_sw),
    '^ck_incident_closed$') \gset
SELECT pg_temp.expect_error(19, 'Закриття без коментаря',
    format($q$UPDATE incident SET status = 'closed', acknowledged_at = now(), acknowledged_by = %s, resolved_at = now(), closed_at = now() WHERE id = %s$q$, :user_eng, :inc_sw),
    '^ck_incident_closed$') \gset
SELECT pg_temp.expect_error(20, 'Ескалація без часу ескалації',
    format($q$UPDATE incident SET status = 'escalated' WHERE id = %s$q$, :inc_sw),
    '^ck_incident_escalated$') \gset
-- сповіщення та журнал
SELECT pg_temp.expect_error(21, 'Невдале сповіщення без тексту помилки',
    format($q$INSERT INTO notification (incident_id, user_id, channel, status) VALUES (%s, %s, 'email', 'failed')$q$, :inc_ups, :user_eng),
    '^ck_notification_failed$') \gset
SELECT pg_temp.expect_error(22, 'Деталі події — JSON-масив, а не об’єкт',
    $q$INSERT INTO event_log (event_type, message, details) VALUES ('auth', 'x', '[1, 2]')$q$,
    '^ck_event_log_details$') \gset
-- збереження історії при видаленні
SELECT pg_temp.expect_error(23, 'Видалення обладнання з інцидентами',
    format($q$DELETE FROM equipment WHERE id = %s$q$, :eq_ups),
    '^fk_incident_equipment$') \gset
SELECT pg_temp.expect_error(24, 'Видалення користувача з історією інцидентів і сповіщень',
    format($q$DELETE FROM users WHERE id = %s$q$, :user_eng),
    '^(fk_incident_ack_user|fk_notification_user)$') \gset
SELECT pg_temp.expect_error(25, 'Видалення ролі, призначеної користувачам',
    format($q$DELETE FROM roles WHERE id = %s$q$, :role_support),
    '^fk_users_role$') \gset
-- конфігурація SNMP (зв'язок із YAML-файлом)
SELECT pg_temp.expect_error(26, 'Дві активні версії YAML-конфігурації',
    $q$INSERT INTO snmp_config (file_path, sha256, format_version, content, is_active) VALUES
       ('/etc/netguard/snmp_config.yaml', repeat('c', 64), 1, '{}', TRUE),
       ('/etc/netguard/snmp_config.yaml', repeat('d', 64), 1, '{}', TRUE)$q$,
    '^uq_snmp_config_active$') \gset
SELECT pg_temp.expect_error(27, 'Повторна реєстрація файлу з тією самою контрольною сумою',
    $q$INSERT INTO snmp_config (file_path, sha256, format_version, content) VALUES ('/etc/netguard/snmp_config.yaml', repeat('a', 64), 1, '{}')$q$,
    '^uq_snmp_config_sha256$') \gset
SELECT pg_temp.expect_error(28, 'Контрольна сума не у форматі SHA-256 (hex)',
    $q$INSERT INTO snmp_config (file_path, sha256, format_version, content) VALUES ('/etc/netguard/snmp_config.yaml', 'XYZ', 1, '{}')$q$,
    '^ck_snmp_config_sha256$') \gset
SELECT pg_temp.expect_error(29, 'Файл конфігурації не YAML',
    $q$INSERT INTO snmp_config (file_path, sha256, format_version, content) VALUES ('/etc/netguard/snmp.json', repeat('e', 64), 1, '{}')$q$,
    '^ck_snmp_config_path$') \gset
SELECT pg_temp.expect_error(30, 'SNMP v3 без імені користувача',
    format($q$INSERT INTO snmp_credential (config_id, name, snmp_version, secret_ref) VALUES (%s, 't-bad-v3', 'v3', 'SNMP_X')$q$, :cfg),
    '^ck_snmp_credential_v3$') \gset
SELECT pg_temp.expect_error(31, 'Відкритий секрет замість імені змінної середовища',
    format($q$INSERT INTO snmp_credential (config_id, name, snmp_version, secret_ref) VALUES (%s, 't-plain', 'v2c', 'public')$q$, :cfg),
    '^ck_snmp_credential_secret$') \gset
SELECT pg_temp.expect_error(32, 'Шифрування v3 без автентифікації',
    format($q$INSERT INTO snmp_credential (config_id, name, snmp_version, username, priv_protocol, secret_ref) VALUES (%s, 't-priv', 'v3', 'u', 'AES', 'SNMP_X')$q$, :cfg),
    '^ck_snmp_credential_priv$') \gset
SELECT pg_temp.expect_error(33, 'Протокол автентифікації для SNMP v2c',
    format($q$INSERT INTO snmp_credential (config_id, name, snmp_version, auth_protocol, secret_ref) VALUES (%s, 't-v2-auth', 'v2c', 'SHA', 'SNMP_X')$q$, :cfg),
    '^ck_snmp_credential_auth$') \gset
SELECT pg_temp.expect_error(34, 'OID у символьному форматі',
    format($q$INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, config_id) VALUES (%s, %s, 'iso.3.6.1', %s)$q$, :type_ups, :m_temp, :cfg),
    '^ck_oid_map_oid$') \gset
SELECT pg_temp.expect_error(35, 'Множник OID дорівнює нулю',
    format($q$INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, scale, config_id) VALUES (%s, %s, '1.3.6.1.2.1.1.3.0', 0, %s)$q$, :type_ups, :m_temp, :cfg),
    '^ck_oid_map_scale$') \gset
SELECT pg_temp.expect_error(36, 'Другий OID для тієї самої пари «тип — метрика»',
    format($q$INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, config_id) VALUES (%s, %s, '1.3.6.1.2.1.1.3.0', %s)$q$, :type_ups, :m_charge, :cfg),
    '^pk_snmp_oid_map$') \gset
SELECT pg_temp.expect_error(37, 'Обладнання з неіснуючим обліковим записом SNMP',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (%s, %s, 't-nocred', '192.0.2.96', 32000)$q$, :type_sw, :loc_dc),
    '^fk_equipment_credential$') \gset
SELECT pg_temp.expect_error(38, 'Ключ профілю YAML із пробілом і великими літерами',
    $q$INSERT INTO equipment_type (name, category, snmp_profile) VALUES ('Т: поганий профіль', 'switch', 'Bad Profile')$q$,
    '^ck_equipment_type_profile$') \gset
SELECT pg_temp.expect_error(39, 'Видалення облікового запису, за яким опитуються пристрої',
    format($q$DELETE FROM snmp_credential WHERE id = %s$q$, :cred_net),
    '^fk_equipment_credential$') \gset
SELECT pg_temp.expect_error(40, 'Видалення версії конфігурації, на яку посилаються дані',
    format($q$DELETE FROM snmp_config WHERE id = %s$q$, :cfg),
    '^fk_(snmp_credential|oid_map|equipment)_config$') \gset

-- узгодженість, посилена за результатами тестування (№ 41–51)
SELECT pg_temp.expect_error(41, 'E-mail із великими літерами (обхід унікальності за регістром)',
    format($q$INSERT INTO users (role_id, full_name, login, password_hash, email) VALUES (%s, 'X', 't.upper', 'h', 'T.Admin@ISP.example')$q$, :role_support),
    '^ck_users_email$') \gset
SELECT pg_temp.expect_error(42, 'IPv4-mapped IPv6 — дубль наявної IPv4-адреси',
    format($q$INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES (%s, %s, 't-mapped', '::ffff:192.0.2.10', %s)$q$, :type_sw, :loc_dc, :cred_net),
    '^ck_equipment_ip_host$') \gset
SELECT pg_temp.expect_error(43, 'Новий інцидент із заповненим підтвердженням',
    format($q$INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value, acknowledged_at, acknowledged_by) VALUES (%s, 'threshold', %s, 20, now(), %s)$q$, :eq_ups, :thr_charge, :user_eng),
    '^ck_incident_ack$') \gset
SELECT pg_temp.expect_error(44, 'Новий інцидент із датою закриття',
    format($q$INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value, closed_at) VALUES (%s, 'threshold', %s, 20, now() - INTERVAL '1 day')$q$, :eq_ups, :thr_charge),
    '^ck_incident_closed$') \gset
SELECT pg_temp.expect_error(45, 'Підтвердження раніше за відкриття інциденту',
    format($q$UPDATE incident SET status = 'acknowledged', acknowledged_by = %s, acknowledged_at = opened_at - INTERVAL '1 hour' WHERE id = %s$q$, :user_eng, :inc_sw),
    '^ck_incident_time_order$') \gset
SELECT pg_temp.expect_error(46, 'SNMP v3 з порожнім іменем користувача',
    format($q$INSERT INTO snmp_credential (config_id, name, snmp_version, username, secret_ref) VALUES (%s, 't-empty-user', 'v3', '  ', 'SNMP_X')$q$, :cfg),
    '^ck_snmp_credential_v3$') \gset
SELECT pg_temp.expect_error(47, 'Сповіщення в черзі з часом відправлення',
    format($q$INSERT INTO notification (incident_id, user_id, channel, status, sent_at) VALUES (%s, %s, 'web', 'queued', now())$q$, :inc_ups, :user_eng),
    '^ck_notification_sent$') \gset
SELECT pg_temp.expect_error(48, 'Ескалація пізніше за підтвердження',
    format($q$UPDATE incident SET status = 'acknowledged', escalated_at = opened_at + INTERVAL '30 minutes', acknowledged_at = opened_at + INTERVAL '1 minute', acknowledged_by = %s WHERE id = %s$q$, :user_eng, :inc_sw),
    '^ck_incident_time_order$') \gset
SELECT pg_temp.expect_error(49, 'Коментар закриття лише з табуляції та переведення рядка',
    format($q$UPDATE incident SET status = 'closed', acknowledged_at = now(), acknowledged_by = %s, resolved_at = now(), closed_at = now(), close_comment = E'\t\n' WHERE id = %s$q$, :user_eng, :inc_sw),
    '^ck_incident_closed$') \gset
SELECT pg_temp.expect_error(50, 'Невдале сповіщення з порожнім текстом помилки',
    format($q$INSERT INTO notification (incident_id, user_id, channel, status, error_message) VALUES (%s, %s, 'email', 'failed', '   ')$q$, :inc_ups, :user_eng),
    '^ck_notification_failed$') \gset
SELECT pg_temp.expect_error(51, 'Значення NaN в інциденті порушення порогу',
    format($q$INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value) VALUES (%s, 'threshold', %s, 'NaN')$q$, :eq_ups, :thr_charge),
    '^ck_incident_value$') \gset

-- ---------------------------------------------------------------------
-- 3. Результати
-- ---------------------------------------------------------------------
\echo '--- Негативні перевірки ---'
SELECT no AS "№", check_ AS "Перевірка", actual AS "Відхилено обмеженням", result AS "Результат"
FROM test_result ORDER BY no;

SELECT count(*) FILTER (WHERE result = 'PASS') AS passed,
       count(*) FILTER (WHERE result <> 'PASS') AS failed
FROM test_result;

DO $chk$
BEGIN
    IF (SELECT count(*) FROM test_result) <> 51 OR EXISTS (SELECT 1 FROM test_result WHERE result <> 'PASS') THEN
        RAISE EXCEPTION 'Є непройдені перевірки — див. таблицю вище';
    END IF;
END $chk$;

ROLLBACK;
\echo 'Усі 51 перевірку пройдено; транзакцію відкочено, тестові дані не збережено.'

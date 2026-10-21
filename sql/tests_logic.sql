-- =====================================================================
--  NetGuard Monitor — автотест програмної логіки БД (sql/lr2_logic.sql)
--
--  Запуск (після schema.sql, lr2_logic.sql і seed_reference.sql):
--    psql -d netguard -v ON_ERROR_STOP=1 -f sql/tests_logic.sql
--
--  Кожна перевірка — або умова, що має бути істинною (позитивний сценарій),
--  або дія, яку БД має відхилити з очікуваним повідомленням (негативний).
--  Усе виконується в одній транзакції з відкатом: дані в БД не змінюються.
--  Потрібні права суперкористувача (перемикання ролей ng_*).
-- =====================================================================
\set QUIET on
\set ON_ERROR_STOP on
SET search_path TO netguard, public;
BEGIN;

CREATE TEMP TABLE t_result (no INTEGER PRIMARY KEY, check_name TEXT, detail TEXT, result TEXT) ON COMMIT DROP;

-- умова, що має бути істинною
CREATE FUNCTION pg_temp.ok(p_no INTEGER, p_name TEXT, p_cond BOOLEAN, p_detail TEXT DEFAULT NULL) RETURNS VOID
LANGUAGE sql AS $$
    INSERT INTO t_result VALUES (p_no, p_name, p_detail, CASE WHEN p_cond THEN 'PASS' ELSE 'FAIL' END)
$$;

-- дія, що має завершитися помилкою з повідомленням або обмеженням, яке відповідає p_expected
CREATE FUNCTION pg_temp.fails(p_no INTEGER, p_name TEXT, p_sql TEXT, p_expected TEXT) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_msg TEXT; v_con TEXT;
BEGIN
    BEGIN
        EXECUTE p_sql;
        INSERT INTO t_result VALUES (p_no, p_name, '(виконано без помилки)', 'FAIL');
        RETURN;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT, v_con = CONSTRAINT_NAME;
    END;
    INSERT INTO t_result VALUES (p_no, p_name, left(coalesce(nullif(v_con, '') || ': ', '') || v_msg, 90),
        CASE WHEN coalesce(v_con, '') || ' ' || v_msg ~* p_expected THEN 'PASS' ELSE 'FAIL' END);
END $$;

-- ---------------------------------------------------------------- тестові дані (префікс «t»)
INSERT INTO roles (code, name) VALUES ('support', 'Сапорт'), ('admin', 'Адміністратор') ON CONFLICT (code) DO NOTHING;
INSERT INTO users (role_id, full_name, login, password_hash, email, telegram_chat_id, is_active) VALUES
    ((SELECT id FROM roles WHERE code = 'support'), 'Тест Інженер',  't.eng',   fn_hash_password('Secret#1'), 't.eng@test.example', 990001, TRUE),
    ((SELECT id FROM roles WHERE code = 'support'), 'Тест Блок',     't.block', fn_hash_password('Secret#1'), NULL, NULL, FALSE),
    ((SELECT id FROM roles WHERE code = 'admin'),   'Тест Адмін',    't.adm',   fn_hash_password('Secret#1'), NULL, 990002, TRUE);
INSERT INTO locations (name, location_type, address) VALUES ('Т: вузол', 'access_node', 'тестова адреса');
INSERT INTO metric (code, name, unit) VALUES ('t_temp', 'Тест: температура', '°C'), ('t_charge', 'Тест: заряд', '%');
INSERT INTO equipment_type (name, category, snmp_profile) VALUES ('Т: комутатор', 'switch', 't-sw'), ('Т: ДБЖ', 'ups', 't-ups');
INSERT INTO snmp_config (file_path, sha256, format_version, content, is_active)
SELECT '/etc/netguard/t.yaml', repeat('b', 64), 1, '{"defaults": {"offline_after_failures": 3}}', NOT EXISTS (SELECT 1 FROM snmp_config WHERE is_active);

SELECT (SELECT id FROM users WHERE login = 't.eng') AS u_eng, (SELECT id FROM users WHERE login = 't.block') AS u_block,
       (SELECT id FROM users WHERE login = 't.adm') AS u_adm, (SELECT id FROM locations WHERE name = 'Т: вузол') AS loc,
       (SELECT id FROM metric WHERE code = 't_temp') AS m_temp, (SELECT id FROM metric WHERE code = 't_charge') AS m_charge,
       (SELECT id FROM equipment_type WHERE snmp_profile = 't-sw') AS type_sw,
       (SELECT id FROM equipment_type WHERE snmp_profile = 't-ups') AS type_ups,
       (SELECT id FROM snmp_config WHERE sha256 = repeat('b', 64)) AS cfg \gset
INSERT INTO snmp_credential (config_id, name, snmp_version, secret_ref) VALUES (:cfg, 't-v2c', 'v2c', 'T_COMMUNITY');
SELECT id AS cred FROM snmp_credential WHERE name = 't-v2c' \gset
INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id) VALUES
    (:type_sw, :loc, 't-sw1', '198.51.100.1', :cred), (:type_sw, :loc, 't-sw2', '198.51.100.2', :cred),
    (:type_sw, :loc, 't-off', '198.51.100.3', :cred);
UPDATE equipment SET is_active = FALSE WHERE name = 't-off';
UPDATE equipment SET config_id = :cfg WHERE name = 't-sw2';   -- керується YAML-файлом
SELECT (SELECT id FROM equipment WHERE name = 't-sw1') AS sw1, (SELECT id FROM equipment WHERE name = 't-sw2') AS sw2,
       (SELECT id FROM equipment WHERE name = 't-off') AS swoff \gset
INSERT INTO threshold (equipment_type_id, metric_id, severity, max_value) VALUES (:type_sw, :m_temp, 'warning', 60);
INSERT INTO threshold (equipment_type_id, metric_id, severity, min_value) VALUES (:type_ups, :m_charge, 'warning', 50);
SELECT (SELECT id FROM threshold WHERE equipment_type_id = :type_sw) AS thr_sw,
       (SELECT id FROM threshold WHERE equipment_type_id = :type_ups) AS thr_ups \gset

-- ---------------------------------------------------------------- 1–3. автентифікація
SELECT pg_temp.ok(1, 'Вхід з правильним паролем повертає користувача і роль',
    (SELECT role_code FROM fn_authenticate('T.ENG', 'Secret#1')) = 'support');
-- (зміни, зроблені функцією, видно лише наступним операторам, тому виклик і перевірка — окремо)
SELECT count(*) AS wrong_rows FROM fn_authenticate('t.eng', 'wrong') \gset
SELECT pg_temp.ok(2, 'Неправильний пароль: вхід відхилено, подію записано в журнал',
    :wrong_rows = 0
    AND EXISTS (SELECT 1 FROM event_log WHERE event_type = 'auth' AND message = 'Невдала спроба входу' AND details ->> 'login' = 't.eng'));
SELECT pg_temp.ok(3, 'Заблокований користувач не може увійти навіть з правильним паролем',
    NOT EXISTS (SELECT 1 FROM fn_authenticate('t.block', 'Secret#1')));

-- ---------------------------------------------------------------- 4–9. перевірка порогів і сповіщення
INSERT INTO metric_reading VALUES (:sw1, :m_temp, now() - INTERVAL '2 minutes', 64.5);
SELECT id AS inc1 FROM incident WHERE equipment_id = :sw1 \gset
SELECT pg_temp.ok(4, 'Показник поза порогом автоматично відкриває інцидент зі значенням',
    (SELECT status = 'new' AND metric_value = 64.5 AND threshold_id = :thr_sw FROM incident WHERE id = :inc1));
INSERT INTO metric_reading VALUES (:sw1, :m_temp, now() - INTERVAL '1 minute', 66.0);
SELECT pg_temp.ok(5, 'Повторне порушення не дублює відкритий інцидент',
    (SELECT count(*) FROM incident WHERE equipment_id = :sw1) = 1);
INSERT INTO metric_reading VALUES (:sw2, :m_temp, now(), 45.0), (:swoff, :m_temp, now(), 90.0);
SELECT pg_temp.ok(6, 'Показник у нормі та показник неактивного пристрою інцидентів не створюють',
    NOT EXISTS (SELECT 1 FROM incident WHERE equipment_id IN (:sw2, :swoff)));
SELECT pg_temp.ok(7, 'Сповіщення поставлено в чергу інженеру: web, email, telegram',
    (SELECT string_agg(channel::TEXT, ',' ORDER BY channel) FROM notification
      WHERE incident_id = :inc1 AND user_id = :u_eng AND status = 'queued' AND NOT is_escalation) = 'web,email,telegram');
SELECT pg_temp.ok(8, 'Заблокований інженер сповіщень не отримує',
    NOT EXISTS (SELECT 1 FROM notification WHERE incident_id = :inc1 AND user_id = :u_block));
SELECT pg_temp.ok(9, 'Антифлуд: повторна постановка того самого сповіщення протягом 10 хв не додає рядків',
    fn_enqueue_notifications(:inc1, FALSE) = 0);

-- ---------------------------------------------------------------- 10–14. правила відкриття інциденту
SELECT pg_temp.fails(10, 'Інцидент за порогом іншого типу обладнання',
    format($q$INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value) VALUES (%s, 'threshold', %s, 10)$q$, :sw2, :thr_ups),
    'не належить типу');
SELECT pg_temp.fails(11, 'Новий інцидент одразу в статусі closed',
    format($q$INSERT INTO incident (equipment_id, incident_type, status, closed_at, close_comment, acknowledged_at, acknowledged_by, resolved_at)
              VALUES (%s, 'offline', 'closed', now(), 'x', now(), %s, now())$q$, :sw2, :u_eng),
    'має статус new');
SELECT pg_temp.fails(12, 'Інцидент для неактивного обладнання',
    format($q$INSERT INTO incident (equipment_id, incident_type) VALUES (%s, 'offline')$q$, :swoff), 'активного обладнання');
SELECT pg_temp.fails(13, 'Зміна значення показника в наявному інциденті',
    format($q$UPDATE incident SET metric_value = 1 WHERE id = %s$q$, :inc1), 'змінювати не можна');
SELECT pg_temp.fails(14, 'Ескалаційне сповіщення для неескальованого інциденту',
    format($q$INSERT INTO notification (incident_id, user_id, channel, is_escalation) VALUES (%s, %s, 'web', TRUE)$q$, :inc1, :u_adm),
    'ескальованого');

-- ---------------------------------------------------------------- 15–22. життєвий цикл
SELECT pg_temp.fails(15, 'Пряма зміна статусу new → closed',
    format($q$UPDATE incident SET status = 'closed', acknowledged_at = now(), acknowledged_by = %s, resolved_at = now(),
              closed_at = now(), close_comment = 'x' WHERE id = %s$q$, :u_eng, :inc1), 'Недопустимий перехід');
SELECT pg_temp.fails(16, 'Прийняття інциденту заблокованим користувачем',
    format('CALL sp_acknowledge_incident(%s, %s)', :inc1, :u_block), 'активний користувач');
SELECT pg_temp.fails(17, 'Закриття ще не усунутого інциденту',
    format($q$CALL sp_close_incident(%s, %s, 'коментар')$q$, :inc1, :u_eng), 'лише усунутий');
SELECT fn_escalate_overdue(INTERVAL '0 seconds') AS escalated \gset
SELECT pg_temp.ok(18, 'Ескалація: інцидент new після таймауту стає escalated',
    :escalated >= 1 AND (SELECT status FROM incident WHERE id = :inc1) = 'escalated');
SELECT pg_temp.ok(19, 'Ескалаційне сповіщення адміністратору поставлено в чергу',
    EXISTS (SELECT 1 FROM notification WHERE incident_id = :inc1 AND user_id = :u_adm AND is_escalation));
CALL sp_acknowledge_incident(:inc1, :u_eng);
CALL sp_resolve_incident(:inc1, :u_eng);
SELECT pg_temp.fails(20, 'Закриття з коментарем лише з пробілів',
    format($q$CALL sp_close_incident(%s, %s, '   ')$q$, :inc1, :u_eng), 'ck_incident_closed');
CALL sp_close_incident(:inc1, :u_eng, 'Перевірено охолодження');
SELECT pg_temp.ok(21, 'Повний цикл escalated → acknowledged → resolved → closed пройдено',
    (SELECT status = 'closed' AND acknowledged_by = :u_eng AND close_comment = 'Перевірено охолодження' FROM incident WHERE id = :inc1));
SELECT pg_temp.ok(22, 'Кожен перехід статусу зафіксовано в журналі подій',
    (SELECT count(*) FROM event_log WHERE incident_id = :inc1 AND event_type = 'incident') = 5);

-- ---------------------------------------------------------------- 23–27. облік недоступності (SR-04.2)
CALL sp_register_poll_failure(:sw2);
CALL sp_register_poll_failure(:sw2);
SELECT pg_temp.ok(23, 'Дві невдачі поспіль: статус не змінено, інциденту немає',
    (SELECT failed_polls = 2 AND status <> 'offline' FROM equipment WHERE id = :sw2)
    AND NOT EXISTS (SELECT 1 FROM incident WHERE equipment_id = :sw2));
CALL sp_register_poll_failure(:sw2);
SELECT pg_temp.ok(24, 'Третя невдача: статус offline та інцидент offline',
    (SELECT status FROM equipment WHERE id = :sw2) = 'offline'
    AND (SELECT count(*) FROM incident WHERE equipment_id = :sw2 AND incident_type = 'offline') = 1);
CALL sp_register_poll_failure(:sw2);
SELECT pg_temp.ok(25, 'Четверта невдача не дублює інцидент offline',
    (SELECT count(*) FROM incident WHERE equipment_id = :sw2) = 1);
CALL sp_register_poll_success(:sw2);
SELECT pg_temp.ok(26, 'Успішне опитування: online, лічильник скинуто, час відповіді оновлено',
    (SELECT status = 'online' AND failed_polls = 0 AND last_seen_at IS NOT NULL FROM equipment WHERE id = :sw2));
SELECT pg_temp.ok(27, 'Зміни стану online ↔ offline записано в журнал (status_change)',
    (SELECT count(*) FROM event_log WHERE equipment_id = :sw2 AND event_type = 'status_change') = 2);

-- ---------------------------------------------------------------- 28–30. ДБЖ
SELECT pg_temp.fails(28, 'Паспорт ДБЖ для комутатора',
    format('INSERT INTO ups_details (equipment_id, rated_power_va, battery_capacity_ah) VALUES (%s, 1000, 7)', :sw1),
    'лише обладнанню категорії ups');
SELECT fn_register_ups(:type_ups::SMALLINT, :loc, 't-ups1', '198.51.100.10', :cred::SMALLINT, 20000, 9.0, 20000) AS ups1 \gset
SELECT pg_temp.ok(29, 'fn_register_ups: обладнання і паспорт ДБЖ створено однією дією',
    EXISTS (SELECT 1 FROM ups_details WHERE equipment_id = :ups1 AND rated_power_va = 20000));
SELECT pg_temp.fails(30, 'fn_register_ups з активною потужністю більшою за повну: відкат усієї дії',
    format($q$SELECT fn_register_ups(%s::SMALLINT, %s, 't-ups2', '198.51.100.11', %s::SMALLINT, 1000, 7, 2000)$q$, :type_ups, :loc, :cred),
    'ck_ups_power_w');

-- ---------------------------------------------------------------- 31–33. аудит змін
SELECT set_config('netguard.user_id', :'u_adm', true) \gset
UPDATE threshold SET max_value = 62 WHERE id = :thr_sw;
SELECT pg_temp.ok(31, 'Зміна порогу: автор і значення «було/стало» в журналі',
    EXISTS (SELECT 1 FROM event_log WHERE event_type = 'config_change' AND user_id = :u_adm
             AND (details -> 'max_value' ->> 'було')::NUMERIC = 60 AND (details -> 'max_value' ->> 'стало')::NUMERIC = 62));
UPDATE equipment SET poll_interval_s = 30 WHERE id = :sw1;
SELECT pg_temp.ok(32, 'Зміна обладнання користувачем записується як user_action з різницею',
    EXISTS (SELECT 1 FROM event_log WHERE event_type = 'user_action' AND equipment_id = :sw1 AND user_id = :u_adm
             AND details -> 'poll_interval_s' ->> 'стало' = '30'));
UPDATE users SET password_hash = fn_hash_password('Secret#2') WHERE id = :u_eng;
SELECT pg_temp.ok(33, 'Зміна пароля в журналі без самого хешу',
    EXISTS (SELECT 1 FROM event_log WHERE message = 'Змінено користувача t.eng' AND details ->> 'password_hash' = 'змінено'));
SELECT set_config('netguard.user_id', '', true) \gset

-- ---------------------------------------------------------------- 34–37. застосування YAML-конфігурації
SELECT sp_apply_snmp_config('/etc/netguard/t.yml', repeat('c', 64),
        '{"version": 1, "defaults": {"port": 161, "poll_interval_s": 60},
          "credentials": {"t-yaml": {"version": "v2c", "secret": "${T_YAML}"}},
          "profiles": {"t-sw": {"name": "Т: комутатор", "category": "switch", "metrics": {"t_temp": {"oid": "1.3.6.1.4.1.9.9.13.1.3.1.3.1"}}}},
          "devices": [{"name": "t-sw1", "ip": "198.51.100.1", "location": "Т: вузол", "profile": "t-sw", "credential": "t-yaml"}]}') AS applied \gset
SELECT pg_temp.ok(34, 'sp_apply_snmp_config: нова версія застосована й активна',
    :'applied' = 'applied' AND (SELECT is_active FROM snmp_config WHERE sha256 = repeat('c', 64)));
SELECT pg_temp.ok(35, 'Повторне застосування того самого вмісту — пропуск',
    sp_apply_snmp_config('/etc/netguard/t.yml', repeat('c', 64), '{}') = 'skipped');
SELECT pg_temp.ok(36, 'Пристрій, якого немає у файлі, деактивовано; пристрій із файлу синхронізовано',
    (SELECT is_active FROM equipment WHERE id = :sw2) = FALSE
    AND (SELECT c.name FROM equipment e JOIN snmp_credential c ON c.id = e.credential_id WHERE e.id = :sw1) = 't-yaml');
SELECT pg_temp.fails(37, 'YAML з невідомим вузлом: зрозуміла помилка, відкат',
    $q$SELECT sp_apply_snmp_config('/etc/netguard/t.yml', repeat('d', 64),
        '{"version": 1, "defaults": {"port": 161, "poll_interval_s": 60},
          "credentials": {"t-yaml": {"version": "v2c", "secret": "${T_YAML}"}},
          "profiles": {"t-sw": {"name": "Т: комутатор", "category": "switch", "metrics": {"t_temp": {"oid": "1.3.6.1"}}}},
          "devices": [{"name": "t-x", "ip": "198.51.100.77", "location": "Немає такого", "profile": "t-sw", "credential": "t-yaml"}]}')$q$,
    'невідомий вузол');

-- ---------------------------------------------------------------- 38–40. ролі СУБД
SELECT pg_temp.fails(38, 'Роль звітів ng_report не може змінювати дані',
    $q$DO $d$ BEGIN SET LOCAL ROLE ng_report; UPDATE netguard.threshold SET max_value = 1; END $d$$q$, 'permission denied');
SELECT pg_temp.fails(39, 'Сервіс сповіщень ng_notifier не може змінити канал сповіщення',
    $q$DO $d$ BEGIN SET LOCAL ROLE ng_notifier; UPDATE netguard.notification SET channel = 'web'; END $d$$q$, 'permission denied');
SELECT pg_temp.fails(40, 'Сервіс опитування ng_poller не може змінювати користувачів',
    $q$DO $d$ BEGIN SET LOCAL ROLE ng_poller; UPDATE netguard.users SET is_active = TRUE; END $d$$q$, 'permission denied');
RESET ROLE;

-- ---------------------------------------------------------------- 41–44. секції, звіти, графіки
INSERT INTO metric_reading VALUES (:sw1, :m_temp, date_trunc('month', now()) + INTERVAL '14 months 1 day', 40);
SELECT fn_create_partitions(15) AS created \gset
SELECT pg_temp.ok(41, 'fn_create_partitions створює відсутні секції й переносить рядки із секції default',
    :created >= 2
    AND to_regclass('netguard.metric_reading_' || to_char(date_trunc('month', now()) + INTERVAL '14 months', 'YYYY_MM')) IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM metric_reading_default WHERE equipment_id = :sw1));
SELECT pg_temp.ok(42, 'Звіт доступності: пристрій без інцидентів offline — 100 %',
    (SELECT uptime_pct FROM fn_report_uptime(now() - INTERVAL '1 day', now()) WHERE equipment_id = :sw1) = 100);
SELECT pg_temp.ok(43, 'Звіт інцидентів рахує MTTA і MTTR закритих інцидентів',
    EXISTS (SELECT 1 FROM fn_report_incidents(now() - INTERVAL '1 day', now() + INTERVAL '1 minute')
             WHERE location = 'Т: вузол' AND avg_mtta IS NOT NULL AND avg_mttr IS NOT NULL));
SELECT pg_temp.ok(44, 'Історія показника агрегується за інтервалами для графіка',
    (SELECT count(*) FROM fn_metric_history(:sw1, 't_temp', now() - INTERVAL '1 hour', now() + INTERVAL '1 minute', INTERVAL '1 minute')) = 2);

-- ---------------------------------------------------------------- підсумок
\set QUIET off
SELECT no AS "№", check_name AS "Перевірка", detail AS "Деталі", result AS "Результат" FROM t_result ORDER BY no;
DO $$
DECLARE
    v_fail INTEGER := (SELECT count(*) FROM t_result WHERE result <> 'PASS');
BEGIN
    IF v_fail > 0 OR (SELECT count(*) FROM t_result) <> 44 THEN
        RAISE EXCEPTION 'Є непройдені перевірки (% з %) — див. таблицю вище', v_fail, (SELECT count(*) FROM t_result);
    END IF;
END $$;
\echo 'Усі 44 перевірки програмної логіки пройдено; транзакцію відкочено, тестові дані не збережено.'
ROLLBACK;

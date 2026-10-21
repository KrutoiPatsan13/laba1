-- =====================================================================
--  NetGuard Monitor — програмна логіка БД (функції, процедури, тригери,
--  представлення, ролі СУБД). Виконується після sql/schema.sql:
--
--    psql -d netguard -v ON_ERROR_STOP=1 -f sql/lr2_logic.sql
--
--  Скрипт можна виконувати повторно: усі об'єкти створюються через
--  CREATE OR REPLACE / DROP ... IF EXISTS.
--
--  Хто виконав дію, БД дізнається зі змінної сеансу netguard.user_id, яку
--  застосунок встановлює на початку кожної транзакції:
--      SELECT set_config('netguard.user_id', '7', true);
-- =====================================================================

SET search_path TO netguard;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;   -- bcrypt: crypt(), gen_salt('bf')

-- ---------------------------------------------------------------------
-- 1. Допоміжні функції
-- ---------------------------------------------------------------------

-- ідентифікатор користувача, від імені якого працює поточна транзакція (NULL — система)
CREATE OR REPLACE FUNCTION fn_current_user_id() RETURNS INTEGER
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT NULLIF(current_setting('netguard.user_id', true), '')::INTEGER
$$;

-- обхід перевірок життєвого циклу дозволено лише суперкористувачу (завантаження
-- історичних даних); ролі застосунку ng_* цього зробити не можуть
CREATE OR REPLACE FUNCTION fn_bypass_checks() RETURNS BOOLEAN
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT coalesce(current_setting('netguard.bypass_checks', true), '') = 'on'
           AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
$$;

-- запис у журнал подій від імені поточного користувача
CREATE OR REPLACE FUNCTION fn_log_event(p_type event_kind, p_message TEXT, p_details JSONB DEFAULT NULL,
                                        p_equipment INTEGER DEFAULT NULL, p_incident BIGINT DEFAULT NULL)
RETURNS VOID LANGUAGE sql SET search_path = netguard, public
AS $$
    INSERT INTO event_log (event_type, user_id, equipment_id, incident_id, message, details)
    VALUES (p_type, fn_current_user_id(), p_equipment, p_incident, left(p_message, 500), p_details)
$$;

-- числовий параметр розділу defaults активної YAML-конфігурації (напр. offline_after_failures)
CREATE OR REPLACE FUNCTION fn_config_default(p_key TEXT, p_fallback INTEGER) RETURNS INTEGER
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT coalesce((SELECT (content -> 'defaults' ->> p_key)::INTEGER FROM snmp_config WHERE is_active),
                    p_fallback)
$$;

-- ---------------------------------------------------------------------
-- 2. Автентифікація (FR-01): bcrypt-хеш через pgcrypto
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_hash_password(p_password TEXT) RETURNS TEXT
LANGUAGE sql VOLATILE SET search_path = netguard, public AS $$
    SELECT crypt(p_password, gen_salt('bf', 10))
$$;

-- повертає користувача, якщо логін і пароль правильні й обліковий запис активний;
-- кожна спроба фіксується в журналі подій (auth)
CREATE OR REPLACE FUNCTION fn_authenticate(p_login TEXT, p_password TEXT)
RETURNS TABLE (user_id INTEGER, full_name TEXT, role_code TEXT)
LANGUAGE plpgsql VOLATILE SET search_path = netguard, public AS $$
DECLARE
    u RECORD;
BEGIN
    SELECT us.id, us.full_name, us.password_hash, us.is_active, r.code
      INTO u
      FROM users us JOIN roles r ON r.id = us.role_id
     WHERE us.login = lower(p_login);

    IF NOT FOUND OR u.password_hash <> crypt(p_password, u.password_hash) THEN
        INSERT INTO event_log (event_type, user_id, message, details)
        VALUES ('auth', u.id, 'Невдала спроба входу', jsonb_build_object('login', lower(p_login)));
        RETURN;
    END IF;
    IF NOT u.is_active THEN
        INSERT INTO event_log (event_type, user_id, message, details)
        VALUES ('auth', u.id, 'Вхід заблокованого користувача відхилено', jsonb_build_object('login', lower(p_login)));
        RETURN;
    END IF;

    UPDATE users SET last_login_at = now() WHERE id = u.id;
    INSERT INTO event_log (event_type, user_id, message) VALUES ('auth', u.id, 'Успішний вхід');
    RETURN QUERY SELECT u.id, u.full_name::TEXT, u.code::TEXT;
END $$;

-- ---------------------------------------------------------------------
-- 3. Обладнання: реєстрація ДБЖ, перевірка категорії, аудит змін
-- ---------------------------------------------------------------------

-- паспорт ДБЖ дозволений лише для обладнання категорії ups (БП-05)
CREATE OR REPLACE FUNCTION trg_ups_details_category() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
BEGIN
    IF NOT fn_bypass_checks() AND NOT EXISTS (SELECT 1 FROM equipment e JOIN equipment_type t ON t.id = e.type_id
                                              WHERE e.id = NEW.equipment_id AND t.category = 'ups') THEN
        RAISE EXCEPTION 'Паспорт ДБЖ можна додати лише обладнанню категорії ups (equipment_id = %)', NEW.equipment_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ups_details_category ON ups_details;
CREATE TRIGGER ups_details_category BEFORE INSERT OR UPDATE OF equipment_id ON ups_details
    FOR EACH ROW EXECUTE FUNCTION trg_ups_details_category();

-- реєстрація ДБЖ однією транзакцією: обладнання + паспорт (FR-03)
CREATE OR REPLACE FUNCTION fn_register_ups(
    p_type_id SMALLINT, p_location_id INTEGER, p_name TEXT, p_ip INET, p_credential_id SMALLINT,
    p_rated_power_va INTEGER, p_battery_capacity_ah NUMERIC, p_rated_power_w INTEGER DEFAULT NULL,
    p_battery_count SMALLINT DEFAULT 1, p_nominal_voltage_v NUMERIC DEFAULT NULL,
    p_model TEXT DEFAULT NULL, p_serial TEXT DEFAULT NULL, p_battery_installed_on DATE DEFAULT NULL)
RETURNS INTEGER LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_id INTEGER;
BEGIN
    INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, model, serial_number)
    VALUES (p_type_id, p_location_id, p_name, p_ip, p_credential_id, p_model, p_serial)
    RETURNING id INTO v_id;
    INSERT INTO ups_details (equipment_id, rated_power_va, rated_power_w, battery_capacity_ah,
                             battery_count, nominal_voltage_v, battery_installed_on)
    VALUES (v_id, p_rated_power_va, p_rated_power_w, p_battery_capacity_ah,
            p_battery_count, p_nominal_voltage_v, p_battery_installed_on);
    RETURN v_id;
END $$;

-- аудит обладнання: зміна стану (status_change) і змін, зроблених користувачем (user_action);
-- під час застосування YAML (netguard.sync = on) окремі рядки не журналюються —
-- застосування фіксується однією подією config_change
CREATE OR REPLACE FUNCTION trg_equipment_audit() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_sync BOOLEAN := coalesce(current_setting('netguard.sync', true), '') = 'on';
    v_diff JSONB;
BEGIN
    IF fn_bypass_checks() THEN
        RETURN coalesce(NEW, OLD);              -- завантаження історичних даних
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT v_sync THEN
            PERFORM fn_log_event('user_action', 'Зареєстровано обладнання ' || NEW.name,
                                 jsonb_build_object('ip', host(NEW.ip_address)), NEW.id);
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM fn_log_event('user_action', 'Видалено обладнання ' || OLD.name,
                             jsonb_build_object('equipment_id', OLD.id, 'ip', host(OLD.ip_address)));
        RETURN OLD;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM fn_log_event('status_change',
                             format('%s: %s → %s', NEW.name, OLD.status, NEW.status),
                             jsonb_build_object('from', OLD.status, 'to', NEW.status), NEW.id);
    END IF;

    IF NOT v_sync THEN
        SELECT jsonb_object_agg(n.key, jsonb_build_object('було', o.value, 'стало', n.value))
          INTO v_diff
          FROM jsonb_each(to_jsonb(NEW) - ARRAY['status', 'failed_polls', 'last_seen_at']) n
          JOIN jsonb_each(to_jsonb(OLD)) o USING (key)
         WHERE n.value IS DISTINCT FROM o.value;
        IF v_diff IS NOT NULL THEN
            PERFORM fn_log_event('user_action', 'Змінено обладнання ' || NEW.name, v_diff, NEW.id);
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS equipment_audit ON equipment;
CREATE TRIGGER equipment_audit AFTER INSERT OR UPDATE OR DELETE ON equipment
    FOR EACH ROW EXECUTE FUNCTION trg_equipment_audit();

-- ---------------------------------------------------------------------
-- 4. Порогові значення: час зміни й аудит «було/стало» (FR-06, FR-09)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_threshold_touch() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS threshold_touch ON threshold;
CREATE TRIGGER threshold_touch BEFORE UPDATE ON threshold
    FOR EACH ROW EXECUTE FUNCTION trg_threshold_touch();

CREATE OR REPLACE FUNCTION trg_threshold_audit() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_name TEXT;
    v_details JSONB;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT jsonb_object_agg(n.key, jsonb_build_object('було', o.value, 'стало', n.value))
          INTO v_details
          FROM jsonb_each(to_jsonb(NEW) - 'updated_at') n
          JOIN jsonb_each(to_jsonb(OLD)) o USING (key)
         WHERE n.value IS DISTINCT FROM o.value;
        IF v_details IS NULL THEN
            RETURN NULL;                         -- змінився лише час (повторне збереження)
        END IF;
    ELSE
        v_details := jsonb_build_object(CASE TG_OP WHEN 'INSERT' THEN 'стало' ELSE 'було' END,
                                        to_jsonb(coalesce(NEW, OLD)) - 'updated_at' - 'id');
    END IF;
    SELECT t.name || ' / ' || m.code INTO v_name
      FROM equipment_type t, metric m
     WHERE t.id = coalesce(NEW.equipment_type_id, OLD.equipment_type_id)
       AND m.id = coalesce(NEW.metric_id, OLD.metric_id);
    PERFORM fn_log_event('config_change',
        CASE TG_OP WHEN 'INSERT' THEN 'Додано поріг ' WHEN 'UPDATE' THEN 'Змінено поріг ' ELSE 'Видалено поріг ' END || v_name,
        jsonb_build_object('threshold_id', coalesce(NEW.id, OLD.id)) || v_details);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS threshold_audit ON threshold;
CREATE TRIGGER threshold_audit AFTER INSERT OR UPDATE OR DELETE ON threshold
    FOR EACH ROW EXECUTE FUNCTION trg_threshold_audit();

-- ---------------------------------------------------------------------
-- 5. Користувачі: аудит дій адміністратора (FR-11); хеш пароля в журнал не потрапляє
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_users_audit() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_diff JSONB;
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM fn_log_event('user_action', 'Створено користувача ' || NEW.login,
                             jsonb_build_object('role_id', NEW.role_id));
        RETURN NULL;
    END IF;
    SELECT jsonb_object_agg(n.key, CASE WHEN n.key = 'password_hash' THEN to_jsonb('змінено'::TEXT)
                                        ELSE jsonb_build_object('було', o.value, 'стало', n.value) END)
      INTO v_diff
      FROM jsonb_each(to_jsonb(NEW) - 'last_login_at') n
      JOIN jsonb_each(to_jsonb(OLD)) o USING (key)
     WHERE n.value IS DISTINCT FROM o.value;
    IF v_diff IS NOT NULL THEN
        PERFORM fn_log_event('user_action', 'Змінено користувача ' || NEW.login, v_diff);
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS users_audit ON users;
CREATE TRIGGER users_audit AFTER INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION trg_users_audit();

-- ---------------------------------------------------------------------
-- 6. Перевірка порогів під час запису показника (FR-06, FR-07)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_reading_check_thresholds() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    th RECORD;
BEGIN
    IF fn_bypass_checks() THEN
        RETURN NULL;                             -- завантаження історичних даних
    END IF;
    FOR th IN
        SELECT t.id
          FROM equipment e
          JOIN threshold t ON t.equipment_type_id = e.type_id AND t.metric_id = NEW.metric_id AND t.is_active
         WHERE e.id = NEW.equipment_id AND e.is_active
           AND ((t.min_value IS NOT NULL AND NEW.value < t.min_value)
             OR (t.max_value IS NOT NULL AND NEW.value > t.max_value))
    LOOP
        -- відкритий інцидент за цим порогом уже є — новий не створюється (uq_incident_open)
        INSERT INTO incident (equipment_id, incident_type, threshold_id, metric_value)
        VALUES (NEW.equipment_id, 'threshold', th.id, NEW.value)
        ON CONFLICT (equipment_id, incident_type, threshold_id)
            WHERE status IN ('new', 'escalated', 'acknowledged') DO NOTHING;
    END LOOP;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS reading_check_thresholds ON metric_reading;
CREATE TRIGGER reading_check_thresholds AFTER INSERT ON metric_reading
    FOR EACH ROW EXECUTE FUNCTION trg_reading_check_thresholds();

-- ---------------------------------------------------------------------
-- 7. Результати опитування (SR-04.1, SR-04.2)
-- ---------------------------------------------------------------------
-- пристрій відповів: скинути лічильник невдач, позначити online
CREATE OR REPLACE PROCEDURE sp_register_poll_success(p_equipment INTEGER)
LANGUAGE sql SET search_path = netguard, public
AS $$
    UPDATE equipment
       SET failed_polls = 0,
           last_seen_at = now(),
           status = CASE WHEN status = 'maintenance' THEN status ELSE 'online'::equipment_status END
     WHERE id = p_equipment
$$;

-- пристрій не відповів: після N невдач поспіль (N = offline_after_failures активної
-- YAML-конфігурації, типово 3) — статус offline та інцидент типу offline
CREATE OR REPLACE PROCEDURE sp_register_poll_failure(p_equipment INTEGER)
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_failed SMALLINT;
    v_status equipment_status;
BEGIN
    UPDATE equipment SET failed_polls = least(failed_polls + 1, 32767)
     WHERE id = p_equipment AND is_active
    RETURNING failed_polls, status INTO v_failed, v_status;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_failed >= fn_config_default('offline_after_failures', 3) AND v_status NOT IN ('offline', 'maintenance') THEN
        UPDATE equipment SET status = 'offline' WHERE id = p_equipment;
        INSERT INTO incident (equipment_id, incident_type)
        VALUES (p_equipment, 'offline')
        ON CONFLICT (equipment_id, incident_type, threshold_id)
            WHERE status IN ('new', 'escalated', 'acknowledged') DO NOTHING;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- 8. Інциденти: узгодженість із порогом, допустимі переходи статусів (FR-08)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_incident_validate() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
BEGIN
    IF fn_bypass_checks() THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'new' THEN
            RAISE EXCEPTION 'Новий інцидент має статус new, а не %', NEW.status USING ERRCODE = 'check_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM equipment WHERE id = NEW.equipment_id AND is_active) THEN
            RAISE EXCEPTION 'Інцидент можна відкрити лише для активного обладнання (equipment_id = %)', NEW.equipment_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.threshold_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM equipment e JOIN threshold t ON t.equipment_type_id = e.type_id
                WHERE e.id = NEW.equipment_id AND t.id = NEW.threshold_id) THEN
            RAISE EXCEPTION 'Поріг % не належить типу обладнання %', NEW.threshold_id, NEW.equipment_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF (NEW.equipment_id, NEW.incident_type, NEW.threshold_id, NEW.metric_value, NEW.opened_at)
       IS DISTINCT FROM (OLD.equipment_id, OLD.incident_type, OLD.threshold_id, OLD.metric_value, OLD.opened_at) THEN
        RAISE EXCEPTION 'Обладнання, тип, поріг, значення та час відкриття інциденту змінювати не можна'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
           ('new', 'escalated'), ('new', 'acknowledged'), ('escalated', 'acknowledged'),
           ('acknowledged', 'resolved'), ('resolved', 'closed')) THEN
        RAISE EXCEPTION 'Недопустимий перехід статусу інциденту: % → %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.acknowledged_by IS DISTINCT FROM OLD.acknowledged_by AND NEW.acknowledged_by IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.acknowledged_by AND is_active) THEN
        RAISE EXCEPTION 'Прийняти інцидент може лише активний користувач' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS incident_validate ON incident;
CREATE TRIGGER incident_validate BEFORE INSERT OR UPDATE ON incident
    FOR EACH ROW EXECUTE FUNCTION trg_incident_validate();

-- процедури дій інженера; кожна блокує рядок інциденту (FOR UPDATE), тож дві одночасні
-- дії над одним інцидентом виконуються послідовно
CREATE OR REPLACE PROCEDURE sp_acknowledge_incident(p_incident BIGINT, p_user INTEGER)
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_status incident_status;
BEGIN
    SELECT status INTO v_status FROM incident WHERE id = p_incident FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Інцидент % не знайдено', p_incident USING ERRCODE = 'no_data_found';
    END IF;
    IF v_status NOT IN ('new', 'escalated') THEN
        RAISE EXCEPTION 'Інцидент % уже прийнято (статус %)', p_incident, v_status USING ERRCODE = 'check_violation';
    END IF;
    UPDATE incident SET status = 'acknowledged', acknowledged_at = now(), acknowledged_by = p_user
     WHERE id = p_incident;
END $$;

CREATE OR REPLACE PROCEDURE sp_resolve_incident(p_incident BIGINT, p_user INTEGER)
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_status incident_status;
BEGIN
    SELECT status INTO v_status FROM incident WHERE id = p_incident FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Інцидент % не знайдено', p_incident USING ERRCODE = 'no_data_found';
    END IF;
    IF v_status <> 'acknowledged' THEN
        RAISE EXCEPTION 'Позначити усунутим можна лише прийнятий інцидент (статус %)', v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user AND is_active) THEN
        RAISE EXCEPTION 'Користувач % не активний', p_user USING ERRCODE = 'check_violation';
    END IF;
    UPDATE incident SET status = 'resolved', resolved_at = now() WHERE id = p_incident;
END $$;

CREATE OR REPLACE PROCEDURE sp_close_incident(p_incident BIGINT, p_user INTEGER, p_comment TEXT)
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_status incident_status;
BEGIN
    SELECT status INTO v_status FROM incident WHERE id = p_incident FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Інцидент % не знайдено', p_incident USING ERRCODE = 'no_data_found';
    END IF;
    IF v_status <> 'resolved' THEN
        RAISE EXCEPTION 'Закрити можна лише усунутий інцидент (статус %)', v_status USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user AND is_active) THEN
        RAISE EXCEPTION 'Користувач % не активний', p_user USING ERRCODE = 'check_violation';
    END IF;
    UPDATE incident SET status = 'closed', closed_at = now(), close_comment = btrim(p_comment)
     WHERE id = p_incident;
END $$;

-- ескалація: інциденти в статусі new, не прийняті за p_after (типово 5 хв), — escalated;
-- повертає кількість ескальованих інцидентів
CREATE OR REPLACE FUNCTION fn_escalate_overdue(p_after INTERVAL DEFAULT INTERVAL '5 minutes')
RETURNS INTEGER LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE incident SET status = 'escalated', escalated_at = now()
     WHERE status = 'new' AND opened_at <= now() - p_after;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;

-- ---------------------------------------------------------------------
-- 9. Сповіщення: черга з антифлуд-перевіркою (FR-07, SR-07.1, SR-07.2)
-- ---------------------------------------------------------------------
-- звичайні сповіщення отримують активні інженери (роль support), ескалаційні — активні
-- адміністратори; канал web — завжди, email і telegram — якщо в користувача є контакт.
-- Сповіщення того самого інциденту тому самому користувачу тим самим каналом не
-- ставиться в чергу частіше ніж раз на 10 хв.
CREATE OR REPLACE FUNCTION fn_enqueue_notifications(p_incident BIGINT, p_escalation BOOLEAN DEFAULT FALSE)
RETURNS INTEGER LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    WITH recipients AS (
        SELECT u.id AS user_id, ch.channel
          FROM users u
          JOIN roles r ON r.id = u.role_id
          CROSS JOIN LATERAL (VALUES ('web'::notify_channel, TRUE),
                                     ('email'::notify_channel, u.email IS NOT NULL),
                                     ('telegram'::notify_channel, u.telegram_chat_id IS NOT NULL)) AS ch(channel, ok)
         WHERE u.is_active AND ch.ok
           AND r.code = CASE WHEN p_escalation THEN 'admin' ELSE 'support' END
    ), queued AS (
        INSERT INTO notification (incident_id, user_id, channel, is_escalation)
        SELECT p_incident, rc.user_id, rc.channel, p_escalation
          FROM recipients rc
         WHERE NOT EXISTS (SELECT 1 FROM notification n
                            WHERE n.incident_id = p_incident AND n.user_id = rc.user_id
                              AND n.channel = rc.channel AND n.created_at > now() - INTERVAL '10 minutes')
        RETURNING id
    )
    SELECT count(*) INTO v_count FROM queued;
    IF v_count > 0 THEN
        PERFORM pg_notify('ng_notification', p_incident::TEXT);   -- будимо Notification Service
    END IF;
    RETURN v_count;
END $$;

-- перевірки сповіщення: отримувач активний, сповіщення не раніше відкриття інциденту,
-- ескалаційне — лише для ескальованого інциденту
CREATE OR REPLACE FUNCTION trg_notification_validate() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_inc RECORD;
BEGIN
    IF fn_bypass_checks() THEN
        RETURN NEW;
    END IF;
    SELECT opened_at, status INTO v_inc FROM incident WHERE id = NEW.incident_id;
    IF NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id AND is_active) THEN
        RAISE EXCEPTION 'Сповіщення можна надіслати лише активному користувачу' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_at < v_inc.opened_at THEN
        RAISE EXCEPTION 'Сповіщення не може бути створене раніше за відкриття інциденту' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.is_escalation AND v_inc.status <> 'escalated' THEN
        RAISE EXCEPTION 'Ескалаційне сповіщення можливе лише для ескальованого інциденту' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_validate ON notification;
CREATE TRIGGER notification_validate BEFORE INSERT ON notification
    FOR EACH ROW EXECUTE FUNCTION trg_notification_validate();

-- події інциденту: журнал + сповіщення (відкриття — інженерам, ескалація — адміністраторам)
CREATE OR REPLACE FUNCTION trg_incident_events() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_eq TEXT;
BEGIN
    IF fn_bypass_checks() THEN
        RETURN NULL;
    END IF;
    SELECT name INTO v_eq FROM equipment WHERE id = NEW.equipment_id;
    IF TG_OP = 'INSERT' THEN
        PERFORM fn_log_event('incident',
            format('Відкрито інцидент №%s (%s) на %s', NEW.id, NEW.incident_type, v_eq),
            jsonb_build_object('threshold_id', NEW.threshold_id, 'value', NEW.metric_value),
            NEW.equipment_id, NEW.id);
        PERFORM fn_enqueue_notifications(NEW.id, FALSE);
        PERFORM pg_notify('ng_incident', NEW.id::TEXT);
    ELSIF NEW.status <> OLD.status THEN
        PERFORM fn_log_event('incident',
            format('Інцидент №%s: %s → %s', NEW.id, OLD.status, NEW.status),
            CASE WHEN NEW.status = 'closed' THEN jsonb_build_object('comment', NEW.close_comment) END,
            NEW.equipment_id, NEW.id);
        IF NEW.status = 'escalated' THEN
            PERFORM fn_enqueue_notifications(NEW.id, TRUE);
        END IF;
        PERFORM pg_notify('ng_incident', NEW.id::TEXT);
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS incident_events ON incident;
CREATE TRIGGER incident_events AFTER INSERT OR UPDATE OF status ON incident
    FOR EACH ROW EXECUTE FUNCTION trg_incident_events();

-- ---------------------------------------------------------------------
-- 10. Застосування YAML-конфігурації SNMP (ЛР4 ТСПП) однією транзакцією.
--     Вміст уже перевірено завантажувачем (tools/snmp_config_sync.py,
--     netguard.snmp_yaml); функція повертає 'skipped', 'reactivated' або 'applied'.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sp_apply_snmp_config(p_path TEXT, p_sha256 TEXT, p_content JSONB,
                                                p_user INTEGER DEFAULT NULL)
RETURNS TEXT LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_known BOOLEAN;
    v_cfg   INTEGER;
    v_type  SMALLINT;
    v_loc   INTEGER;
    v_cred  SMALLINT;
    v_met   SMALLINT;
    v_sec   TEXT;
    v_port  INTEGER := (p_content -> 'defaults' ->> 'port')::INTEGER;
    v_int   INTEGER := (p_content -> 'defaults' ->> 'poll_interval_s')::INTEGER;
    c RECORD; p RECORD; m RECORD; d JSONB;
BEGIN
    IF EXISTS (SELECT 1 FROM snmp_config WHERE sha256 = p_sha256 AND is_active) THEN
        RETURN 'skipped';
    END IF;
    v_known := EXISTS (SELECT 1 FROM snmp_config WHERE sha256 = p_sha256);
    PERFORM set_config('netguard.sync', 'on', true);

    UPDATE snmp_config SET is_active = FALSE WHERE is_active;
    IF v_known THEN
        UPDATE snmp_config SET is_active = TRUE, loaded_at = now(), file_path = p_path, loaded_by = p_user
         WHERE sha256 = p_sha256 RETURNING id INTO v_cfg;
    ELSE
        INSERT INTO snmp_config (file_path, sha256, format_version, content, loaded_by, is_active)
        VALUES (p_path, p_sha256, (p_content ->> 'version')::SMALLINT, p_content, p_user, TRUE)
        RETURNING id INTO v_cfg;
    END IF;

    -- облікові дані: оновлення або вставка за name; секрет — лише ім'я змінної середовища
    FOR c IN SELECT key, value FROM jsonb_each(p_content -> 'credentials') LOOP
        v_sec := substring(c.value ->> 'secret' FROM '^\$\{([A-Z][A-Z0-9_]*)\}$');
        IF v_sec IS NULL THEN
            RAISE EXCEPTION 'credentials.%: секрет має бути посиланням ${ІМ''Я}', c.key USING ERRCODE = 'check_violation';
        END IF;
        UPDATE snmp_credential
           SET config_id = v_cfg, snmp_version = (c.value ->> 'version')::snmp_version,
               username = c.value ->> 'username', auth_protocol = (c.value ->> 'auth_protocol')::snmp_auth_proto,
               priv_protocol = (c.value ->> 'priv_protocol')::snmp_priv_proto, secret_ref = v_sec
         WHERE name = c.key;
        IF NOT FOUND THEN
            INSERT INTO snmp_credential (config_id, name, snmp_version, username, auth_protocol, priv_protocol, secret_ref)
            VALUES (v_cfg, c.key, (c.value ->> 'version')::snmp_version, c.value ->> 'username',
                    (c.value ->> 'auth_protocol')::snmp_auth_proto, (c.value ->> 'priv_protocol')::snmp_priv_proto, v_sec);
        END IF;
    END LOOP;

    -- профілі: тимчасові назви «~id», щоб назви профілів можна було поміняти місцями;
    -- профіль, вилучений з файлу, що досі має назву, яку файл віддає іншому профілю,
    -- отримує назву з позначкою свого ключа (напр. після повернення до старої версії)
    UPDATE equipment_type SET name = '~' || id
     WHERE snmp_profile IN (SELECT jsonb_object_keys(p_content -> 'profiles'));
    UPDATE equipment_type SET name = left(name, 80) || ' (' || snmp_profile || ')'
     WHERE snmp_profile IS NOT NULL
       AND NOT (p_content -> 'profiles') ? snmp_profile
       AND name IN (SELECT value ->> 'name' FROM jsonb_each(p_content -> 'profiles'));
    FOR p IN SELECT key, value FROM jsonb_each(p_content -> 'profiles') LOOP
        UPDATE equipment_type
           SET name = p.value ->> 'name', category = (p.value ->> 'category')::equipment_category,
               vendor = p.value ->> 'vendor', snmp_mib = p.value ->> 'mib'
         WHERE snmp_profile = p.key
        RETURNING id INTO v_type;
        IF NOT FOUND THEN
            INSERT INTO equipment_type (snmp_profile, name, category, vendor, snmp_mib)
            VALUES (p.key, p.value ->> 'name', (p.value ->> 'category')::equipment_category,
                    p.value ->> 'vendor', p.value ->> 'mib')
            RETURNING id INTO v_type;
        END IF;
        DELETE FROM snmp_oid_map WHERE equipment_type_id = v_type;
        FOR m IN SELECT key, value FROM jsonb_each(p.value -> 'metrics') LOOP
            SELECT id INTO v_met FROM metric WHERE code = m.key;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'profiles.%.metrics: невідомий код метрики «%»', p.key, m.key
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            INSERT INTO snmp_oid_map (equipment_type_id, metric_id, oid, scale, config_id)
            VALUES (v_type, v_met, ltrim(m.value ->> 'oid', '.'), coalesce((m.value ->> 'scale')::NUMERIC, 1), v_cfg);
        END LOOP;
    END LOOP;

    -- пристрої: спершу деактивуються всі, керовані файлом (IP можна обмінювати),
    -- потім пристрої з файлу оновлюються або вставляються за name й знову стають активними
    UPDATE equipment SET is_active = FALSE
     WHERE is_active AND (config_id IS NOT NULL
                          OR name IN (SELECT x ->> 'name' FROM jsonb_array_elements(p_content -> 'devices') x));
    FOR d IN SELECT x FROM jsonb_array_elements(p_content -> 'devices') x LOOP
        SELECT id INTO v_loc FROM locations WHERE name = d ->> 'location';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'devices: невідомий вузол «%» (пристрій %)', d ->> 'location', d ->> 'name'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        SELECT id INTO v_type FROM equipment_type WHERE snmp_profile = d ->> 'profile';
        SELECT id INTO v_cred FROM snmp_credential WHERE name = d ->> 'credential';
        UPDATE equipment
           SET type_id = v_type, location_id = v_loc, ip_address = (d ->> 'ip')::INET, credential_id = v_cred,
               config_id = v_cfg, snmp_port = coalesce((d ->> 'port')::INTEGER, v_port),
               poll_interval_s = coalesce((d ->> 'poll_interval_s')::INTEGER, v_int), is_active = TRUE
         WHERE name = d ->> 'name';
        IF NOT FOUND THEN
            INSERT INTO equipment (type_id, location_id, name, ip_address, credential_id, config_id, snmp_port, poll_interval_s)
            VALUES (v_type, v_loc, d ->> 'name', (d ->> 'ip')::INET, v_cred, v_cfg,
                    coalesce((d ->> 'port')::INTEGER, v_port), coalesce((d ->> 'poll_interval_s')::INTEGER, v_int));
        END IF;
    END LOOP;

    -- облікові записи, яких немає у файлі, видаляються, лише якщо ними ніхто не користується
    DELETE FROM snmp_credential sc
     WHERE NOT (p_content -> 'credentials') ? sc.name
       AND NOT EXISTS (SELECT 1 FROM equipment e WHERE e.credential_id = sc.id);

    INSERT INTO event_log (event_type, user_id, message, details)
    VALUES ('config_change', p_user, 'Застосовано YAML-конфігурацію SNMP',
            jsonb_build_object('snmp_config_id', v_cfg, 'sha256', p_sha256, 'reactivated', v_known));
    PERFORM set_config('netguard.sync', '', true);
    RETURN CASE WHEN v_known THEN 'reactivated' ELSE 'applied' END;
END $$;

-- ---------------------------------------------------------------------
-- 11. Представлення та функції для сервісів і звітів
-- ---------------------------------------------------------------------

-- план опитування для Polling Service: один рядок — одна метрика активного пристрою (FR-04)
CREATE OR REPLACE VIEW v_snmp_poll_plan AS
SELECT e.id AS equipment_id, e.name AS equipment_name, host(e.ip_address) AS ip, e.snmp_port, e.poll_interval_s,
       t.snmp_profile, t.category, c.snmp_version, c.username, c.auth_protocol, c.priv_protocol, c.secret_ref,
       m.id AS metric_id, m.code AS metric_code, om.oid, om.scale
  FROM equipment e
  JOIN equipment_type t   ON t.id = e.type_id
  JOIN snmp_credential c  ON c.id = e.credential_id
  JOIN snmp_oid_map om    ON om.equipment_type_id = t.id
  JOIN metric m           ON m.id = om.metric_id
 WHERE e.is_active AND e.status <> 'maintenance';

-- поточний стан обладнання: останнє значення кожної метрики (LATERAL … LIMIT 1 по PK) (FR-05)
CREATE OR REPLACE VIEW v_equipment_current_state AS
SELECT e.id, e.name, host(e.ip_address) AS ip, e.status, e.is_active, e.failed_polls, e.last_seen_at,
       l.id AS location_id, l.name AS location, t.id AS type_id, t.name AS type_name, t.category,
       (SELECT count(*) FROM incident i WHERE i.equipment_id = e.id
                                          AND i.status IN ('new', 'escalated', 'acknowledged')) AS open_incidents,
       (SELECT jsonb_object_agg(m.code, last.value)
          FROM snmp_oid_map om
          JOIN metric m ON m.id = om.metric_id
          CROSS JOIN LATERAL (SELECT r.value FROM metric_reading r
                               WHERE r.equipment_id = e.id AND r.metric_id = om.metric_id
                                 AND r.read_at > now() - INTERVAL '1 day'
                               ORDER BY r.read_at DESC LIMIT 1) last
         WHERE om.equipment_type_id = e.type_id) AS last_values
  FROM equipment e
  JOIN locations l ON l.id = e.location_id
  JOIN equipment_type t ON t.id = e.type_id;

-- інциденти з усіма даними для списку та картки інциденту
CREATE OR REPLACE VIEW v_incident_list AS
SELECT i.id, i.status, i.incident_type, i.opened_at, i.escalated_at, i.acknowledged_at, i.resolved_at, i.closed_at,
       i.close_comment, i.metric_value, i.equipment_id, e.name AS equipment, l.name AS location, l.id AS location_id,
       th.severity, m.code AS metric_code, m.name AS metric_name, m.unit, th.min_value, th.max_value,
       u.full_name AS acknowledged_by_name
  FROM incident i
  JOIN equipment e  ON e.id = i.equipment_id
  JOIN locations l  ON l.id = e.location_id
  LEFT JOIN threshold th ON th.id = i.threshold_id
  LEFT JOIN metric m     ON m.id = th.metric_id
  LEFT JOIN users u      ON u.id = i.acknowledged_by;

-- історія показника для графіка з агрегуванням за інтервалом (FR-12)
CREATE OR REPLACE FUNCTION fn_metric_history(p_equipment INTEGER, p_metric TEXT, p_from TIMESTAMPTZ,
                                             p_to TIMESTAMPTZ, p_bucket INTERVAL DEFAULT INTERVAL '5 minutes')
RETURNS TABLE (bucket TIMESTAMPTZ, avg_value DOUBLE PRECISION, min_value DOUBLE PRECISION, max_value DOUBLE PRECISION)
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT date_bin(p_bucket, r.read_at, TIMESTAMPTZ '2000-01-01') AS bucket,
           avg(r.value), min(r.value), max(r.value)
      FROM metric_reading r
      JOIN metric m ON m.id = r.metric_id AND m.code = p_metric
     WHERE r.equipment_id = p_equipment AND r.read_at >= p_from AND r.read_at < p_to
     GROUP BY 1
     ORDER BY 1
$$;

-- звіт: доступність обладнання за період (простій = тривалість інцидентів offline) (FR-10)
CREATE OR REPLACE FUNCTION fn_report_uptime(p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS TABLE (equipment_id INTEGER, equipment TEXT, location TEXT, category equipment_category,
               downtime INTERVAL, uptime_pct NUMERIC)
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT e.id, e.name, l.name, t.category,
           coalesce(sum(least(coalesce(i.resolved_at, now()), p_to) - greatest(i.opened_at, p_from))
                        FILTER (WHERE i.id IS NOT NULL), INTERVAL '0') AS downtime,
           round(100 * (1 - extract(epoch FROM coalesce(sum(least(coalesce(i.resolved_at, now()), p_to)
                                                            - greatest(i.opened_at, p_from))
                                                            FILTER (WHERE i.id IS NOT NULL), INTERVAL '0'))
                        / extract(epoch FROM p_to - p_from))::NUMERIC, 2)
      FROM equipment e
      JOIN locations l ON l.id = e.location_id
      JOIN equipment_type t ON t.id = e.type_id
      LEFT JOIN incident i ON i.equipment_id = e.id AND i.incident_type = 'offline'
                          AND i.opened_at < p_to AND coalesce(i.resolved_at, now()) > p_from
     WHERE e.is_active
     GROUP BY e.id, e.name, l.name, t.category
     ORDER BY 6, 2
$$;

-- звіт: інциденти за вузлами й категоріями, середні MTTA і MTTR (FR-10)
CREATE OR REPLACE FUNCTION fn_report_incidents(p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS TABLE (location TEXT, category equipment_category, incidents BIGINT, critical BIGINT, offline BIGINT,
               still_open BIGINT, avg_mtta INTERVAL, avg_mttr INTERVAL)
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT l.name, t.category, count(*),
           count(*) FILTER (WHERE th.severity = 'critical'),
           count(*) FILTER (WHERE i.incident_type = 'offline'),
           count(*) FILTER (WHERE i.status IN ('new', 'escalated', 'acknowledged')),
           date_trunc('second', avg(i.acknowledged_at - i.opened_at)),
           date_trunc('second', avg(i.resolved_at - i.opened_at))
      FROM incident i
      JOIN equipment e ON e.id = i.equipment_id
      JOIN locations l ON l.id = e.location_id
      JOIN equipment_type t ON t.id = e.type_id
      LEFT JOIN threshold th ON th.id = i.threshold_id
     WHERE i.opened_at >= p_from AND i.opened_at < p_to
     GROUP BY l.name, t.category
     ORDER BY 3 DESC, 1
$$;

-- звіт: стан АКБ ДБЖ за період
CREATE OR REPLACE FUNCTION fn_report_ups_battery(p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS TABLE (equipment TEXT, location TEXT, rated_power_va INTEGER, battery_capacity_ah NUMERIC,
               battery_age_days INTEGER, charge_last DOUBLE PRECISION, charge_min DOUBLE PRECISION,
               temp_avg DOUBLE PRECISION, runtime_last_min DOUBLE PRECISION)
LANGUAGE sql STABLE SET search_path = netguard, public
AS $$
    SELECT e.name, l.name, u.rated_power_va, u.battery_capacity_ah,
           (current_date - u.battery_installed_on),
           (SELECT r.value FROM metric_reading r JOIN metric m ON m.id = r.metric_id AND m.code = 'battery_charge'
             WHERE r.equipment_id = e.id AND r.read_at >= p_from AND r.read_at < p_to ORDER BY r.read_at DESC LIMIT 1),
           (SELECT min(r.value) FROM metric_reading r JOIN metric m ON m.id = r.metric_id AND m.code = 'battery_charge'
             WHERE r.equipment_id = e.id AND r.read_at >= p_from AND r.read_at < p_to),
           (SELECT round(avg(r.value)::NUMERIC, 1)::DOUBLE PRECISION FROM metric_reading r
              JOIN metric m ON m.id = r.metric_id AND m.code = 'battery_temperature'
             WHERE r.equipment_id = e.id AND r.read_at >= p_from AND r.read_at < p_to),
           (SELECT round((r.value / 60)::NUMERIC, 1)::DOUBLE PRECISION FROM metric_reading r
              JOIN metric m ON m.id = r.metric_id AND m.code = 'battery_runtime'
             WHERE r.equipment_id = e.id AND r.read_at >= p_from AND r.read_at < p_to ORDER BY r.read_at DESC LIMIT 1)
      FROM equipment e
      JOIN equipment_type t ON t.id = e.type_id AND t.category = 'ups'
      JOIN locations l ON l.id = e.location_id
      LEFT JOIN ups_details u ON u.equipment_id = e.id
     WHERE e.is_active
     ORDER BY 7 NULLS LAST, 1
$$;

-- ---------------------------------------------------------------------
-- 12. Обслуговування секцій телеметрії (NFR-02)
-- ---------------------------------------------------------------------
-- створює відсутні місячні секції на p_ahead місяців уперед; рядки з секції за
-- замовчуванням, що потрапляють у нову секцію, переносяться в неї
CREATE OR REPLACE FUNCTION fn_create_partitions(p_ahead INTEGER DEFAULT 12) RETURNS INTEGER
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_start DATE := date_trunc('month', now() AT TIME ZONE 'UTC')::DATE;
    v_end   DATE;
    v_name  TEXT;
    v_count INTEGER := 0;
BEGIN
    FOR i IN 0..p_ahead LOOP
        v_end  := (v_start + INTERVAL '1 month')::DATE;
        v_name := 'metric_reading_' || to_char(v_start, 'YYYY_MM');
        IF to_regclass('netguard.' || v_name) IS NULL THEN
            CREATE TEMP TABLE IF NOT EXISTS _moved (LIKE metric_reading) ON COMMIT DROP;
            EXECUTE format('WITH d AS (DELETE FROM metric_reading_default WHERE read_at >= %L AND read_at < %L
                                       RETURNING *) INSERT INTO _moved SELECT * FROM d', v_start, v_end);
            EXECUTE format('CREATE TABLE netguard.%I PARTITION OF metric_reading FOR VALUES FROM (%L) TO (%L)',
                           v_name, v_start, v_end);
            INSERT INTO metric_reading SELECT * FROM _moved;
            TRUNCATE _moved;
            v_count := v_count + 1;
        END IF;
        v_start := v_end;
    END LOOP;
    RETURN v_count;
END $$;

-- від'єднує й видаляє секції, старші за p_keep місяців; повертає їхні назви
CREATE OR REPLACE FUNCTION fn_drop_old_partitions(p_keep INTEGER DEFAULT 12) RETURNS SETOF TEXT
LANGUAGE plpgsql SET search_path = netguard, public
AS $$
DECLARE
    v_limit DATE := (date_trunc('month', now() AT TIME ZONE 'UTC') - make_interval(months => p_keep))::DATE;
    r RECORD;
BEGIN
    FOR r IN SELECT c.relname
               FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
              WHERE i.inhparent = 'netguard.metric_reading'::regclass
                AND c.relname ~ '^metric_reading_\d{4}_\d{2}$'
                AND to_date(substring(c.relname FROM '\d{4}_\d{2}$'), 'YYYY_MM') < v_limit LOOP
        EXECUTE format('ALTER TABLE metric_reading DETACH PARTITION netguard.%I', r.relname);
        EXECUTE format('DROP TABLE netguard.%I', r.relname);
        RETURN NEXT r.relname;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 13. Ролі СУБД за принципом найменших привілеїв (ФВ «розмежування доступу»)
-- ---------------------------------------------------------------------
DO $$
DECLARE
    r TEXT;
BEGIN
    FOREACH r IN ARRAY ARRAY['ng_web', 'ng_poller', 'ng_alerting', 'ng_notifier', 'ng_report'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA netguard FROM ng_web, ng_poller, ng_alerting, ng_notifier, ng_report;
GRANT USAGE ON SCHEMA netguard TO ng_web, ng_poller, ng_alerting, ng_notifier, ng_report;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA netguard TO ng_web, ng_poller, ng_alerting, ng_notifier, ng_report;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA netguard TO ng_web, ng_poller;

-- звіти та демонстраційні запити: лише читання
GRANT SELECT ON ALL TABLES IN SCHEMA netguard TO ng_report;

-- веб-застосунок (API Gateway): довідники, обладнання, пороги, користувачі, дії з інцидентами
GRANT SELECT ON ALL TABLES IN SCHEMA netguard TO ng_web;
GRANT INSERT, UPDATE, DELETE ON roles, users, locations, equipment, ups_details, threshold TO ng_web;
GRANT INSERT, UPDATE ON incident, snmp_config, snmp_credential, equipment_type, snmp_oid_map TO ng_web;
GRANT DELETE ON snmp_credential, snmp_oid_map TO ng_web;
GRANT INSERT ON event_log, notification TO ng_web;

-- сервіс опитування: читає план, пише показники й стан пристрою; застосовує YAML
GRANT SELECT ON ALL TABLES IN SCHEMA netguard TO ng_poller;
GRANT INSERT ON metric_reading, incident, notification, event_log TO ng_poller;
GRANT UPDATE ON equipment, incident, snmp_config, snmp_credential, equipment_type TO ng_poller;
GRANT INSERT ON snmp_config, snmp_credential, equipment_type, snmp_oid_map, equipment TO ng_poller;
GRANT DELETE ON snmp_credential, snmp_oid_map TO ng_poller;

-- сервіс сповіщень: черга сповіщень і контакти отримувачів
GRANT SELECT ON notification, incident, users, roles, equipment, locations, threshold, metric TO ng_notifier;
GRANT UPDATE (status, sent_at, error_message) ON notification TO ng_notifier;

-- сервіс аналізу аварій: ескалація прострочених інцидентів (сповіщення ставить у чергу БД)
GRANT SELECT ON ALL TABLES IN SCHEMA netguard TO ng_alerting;
GRANT UPDATE ON incident TO ng_alerting;
GRANT INSERT ON notification, event_log TO ng_alerting;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA netguard TO ng_web, ng_poller, ng_alerting, ng_notifier;

-- =====================================================================
--  NetGuard Monitor — фізична модель бази даних
--  СУБД: PostgreSQL 15+ (перевірено на PostgreSQL 16)
--  Лабораторна робота №1, дисципліна «Організація баз даних»
--
--  Запуск:  psql -d netguard -v ON_ERROR_STOP=1 -f sql/schema.sql
-- =====================================================================

DROP SCHEMA IF EXISTS netguard CASCADE;
CREATE SCHEMA netguard;
SET search_path TO netguard;

-- ---------------------------------------------------------------------
-- 1. Користувацькі перелічувані типи (домени зі сталим набором значень)
-- ---------------------------------------------------------------------
CREATE TYPE location_kind      AS ENUM ('data_center', 'access_node');
CREATE TYPE equipment_category AS ENUM ('switch', 'router', 'onu', 'ups');
CREATE TYPE equipment_status   AS ENUM ('unknown', 'online', 'offline', 'maintenance');
CREATE TYPE snmp_version       AS ENUM ('v1', 'v2c', 'v3');
CREATE TYPE severity_level     AS ENUM ('warning', 'critical');
CREATE TYPE incident_kind      AS ENUM ('threshold', 'offline');
CREATE TYPE incident_status    AS ENUM ('new', 'escalated', 'acknowledged', 'resolved', 'closed');
CREATE TYPE notify_channel     AS ENUM ('web', 'email', 'telegram');
CREATE TYPE notify_status      AS ENUM ('queued', 'sent', 'failed');
CREATE TYPE event_kind         AS ENUM ('status_change', 'incident', 'user_action', 'config_change', 'auth');

-- ---------------------------------------------------------------------
-- 2. Довідники та облікові записи
-- ---------------------------------------------------------------------
CREATE TABLE roles (
    id          SMALLINT     GENERATED ALWAYS AS IDENTITY,
    code        VARCHAR(20)  NOT NULL,
    name        VARCHAR(50)  NOT NULL,
    description TEXT,
    CONSTRAINT pk_roles      PRIMARY KEY (id),
    CONSTRAINT uq_roles_code UNIQUE (code),
    CONSTRAINT uq_roles_name UNIQUE (name)
);

CREATE TABLE users (
    id               INTEGER      GENERATED ALWAYS AS IDENTITY,
    role_id          SMALLINT     NOT NULL,
    full_name        VARCHAR(150) NOT NULL,
    login            VARCHAR(50)  NOT NULL,
    password_hash    VARCHAR(100) NOT NULL,
    email            VARCHAR(254),
    telegram_chat_id BIGINT,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_login_at    TIMESTAMPTZ,
    CONSTRAINT pk_users          PRIMARY KEY (id),
    CONSTRAINT fk_users_role     FOREIGN KEY (role_id) REFERENCES roles (id)
                                 ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT uq_users_login    UNIQUE (login),
    CONSTRAINT uq_users_email    UNIQUE (email),
    CONSTRAINT uq_users_telegram UNIQUE (telegram_chat_id),
    CONSTRAINT ck_users_login    CHECK (login ~ '^[a-z0-9_.]{3,50}$'),
    CONSTRAINT ck_users_email    CHECK (email IS NULL OR email ~* '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
);

CREATE TABLE locations (
    id            INTEGER       GENERATED ALWAYS AS IDENTITY,
    name          VARCHAR(100)  NOT NULL,
    location_type location_kind NOT NULL,
    address       VARCHAR(255)  NOT NULL,
    description   TEXT,
    CONSTRAINT pk_locations      PRIMARY KEY (id),
    CONSTRAINT uq_locations_name UNIQUE (name)
);

CREATE TABLE equipment_type (
    id          SMALLINT           GENERATED ALWAYS AS IDENTITY,
    name        VARCHAR(100)       NOT NULL,
    category    equipment_category NOT NULL,
    vendor      VARCHAR(50),
    snmp_mib    VARCHAR(50),
    description TEXT,
    CONSTRAINT pk_equipment_type      PRIMARY KEY (id),
    CONSTRAINT uq_equipment_type_name UNIQUE (name)
);

CREATE TABLE metric (
    id          SMALLINT     GENERATED ALWAYS AS IDENTITY,
    code        VARCHAR(50)  NOT NULL,
    name        VARCHAR(100) NOT NULL,
    unit        VARCHAR(20),
    description TEXT,
    CONSTRAINT pk_metric      PRIMARY KEY (id),
    CONSTRAINT uq_metric_code UNIQUE (code),
    CONSTRAINT ck_metric_code CHECK (code ~ '^[a-z][a-z0-9_]*$')
);

-- ---------------------------------------------------------------------
-- 3. Обладнання
-- ---------------------------------------------------------------------
CREATE TABLE equipment (
    id              INTEGER          GENERATED ALWAYS AS IDENTITY,
    type_id         SMALLINT         NOT NULL,
    location_id     INTEGER          NOT NULL,
    name            VARCHAR(100)     NOT NULL,
    model           VARCHAR(100),
    serial_number   VARCHAR(50),
    ip_address      INET             NOT NULL,
    snmp_port       INTEGER          NOT NULL DEFAULT 161,
    snmp_version    snmp_version     NOT NULL DEFAULT 'v2c',
    snmp_community  VARCHAR(64),
    poll_interval_s SMALLINT         NOT NULL DEFAULT 60,
    status          equipment_status NOT NULL DEFAULT 'unknown',
    failed_polls    SMALLINT         NOT NULL DEFAULT 0,
    last_seen_at    TIMESTAMPTZ,
    is_active       BOOLEAN          NOT NULL DEFAULT TRUE,
    installed_on    DATE,
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT pk_equipment          PRIMARY KEY (id),
    CONSTRAINT fk_equipment_type     FOREIGN KEY (type_id) REFERENCES equipment_type (id)
                                     ON DELETE RESTRICT,
    CONSTRAINT fk_equipment_location FOREIGN KEY (location_id) REFERENCES locations (id)
                                     ON DELETE RESTRICT,
    CONSTRAINT uq_equipment_name     UNIQUE (name),
    CONSTRAINT uq_equipment_serial   UNIQUE (serial_number),
    CONSTRAINT uq_equipment_ip       UNIQUE (ip_address),
    CONSTRAINT ck_equipment_ip_host  CHECK (masklen(ip_address) = CASE family(ip_address) WHEN 4 THEN 32 ELSE 128 END),
    CONSTRAINT ck_equipment_port     CHECK (snmp_port BETWEEN 1 AND 65535),
    CONSTRAINT ck_equipment_interval CHECK (poll_interval_s BETWEEN 10 AND 60),
    CONSTRAINT ck_equipment_failed   CHECK (failed_polls >= 0)
);

-- Підтип «ДБЖ» (зв'язок 1:1 зі спільним первинним ключем)
CREATE TABLE ups_details (
    equipment_id         INTEGER      NOT NULL,
    rated_power_va       INTEGER      NOT NULL,
    rated_power_w        INTEGER,
    battery_capacity_ah  NUMERIC(6,1) NOT NULL,
    battery_count        SMALLINT     NOT NULL DEFAULT 1,
    nominal_voltage_v    NUMERIC(5,1),
    battery_installed_on DATE,
    CONSTRAINT pk_ups_details     PRIMARY KEY (equipment_id),
    CONSTRAINT fk_ups_equipment   FOREIGN KEY (equipment_id) REFERENCES equipment (id)
                                  ON DELETE CASCADE,
    CONSTRAINT ck_ups_power_va    CHECK (rated_power_va > 0),
    CONSTRAINT ck_ups_power_w     CHECK (rated_power_w IS NULL OR rated_power_w BETWEEN 1 AND rated_power_va),
    CONSTRAINT ck_ups_capacity    CHECK (battery_capacity_ah > 0),
    CONSTRAINT ck_ups_battery_cnt CHECK (battery_count > 0),
    CONSTRAINT ck_ups_voltage     CHECK (nominal_voltage_v IS NULL OR nominal_voltage_v > 0)
);

-- ---------------------------------------------------------------------
-- 4. Порогові значення
-- ---------------------------------------------------------------------
CREATE TABLE threshold (
    id                INTEGER        GENERATED ALWAYS AS IDENTITY,
    equipment_type_id SMALLINT       NOT NULL,
    metric_id         SMALLINT       NOT NULL,
    severity          severity_level NOT NULL DEFAULT 'warning',
    min_value         NUMERIC(12,3),
    max_value         NUMERIC(12,3),
    is_active         BOOLEAN        NOT NULL DEFAULT TRUE,
    updated_at        TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT pk_threshold         PRIMARY KEY (id),
    CONSTRAINT fk_threshold_type    FOREIGN KEY (equipment_type_id) REFERENCES equipment_type (id)
                                    ON DELETE CASCADE,
    CONSTRAINT fk_threshold_metric  FOREIGN KEY (metric_id) REFERENCES metric (id)
                                    ON DELETE RESTRICT,
    CONSTRAINT uq_threshold         UNIQUE (equipment_type_id, metric_id, severity),
    CONSTRAINT ck_threshold_bounds  CHECK (min_value IS NOT NULL OR max_value IS NOT NULL),
    CONSTRAINT ck_threshold_range   CHECK (min_value IS NULL OR max_value IS NULL OR min_value < max_value)
);

-- ---------------------------------------------------------------------
-- 5. Телеметрія (секціонована за місяцями таблиця)
-- ---------------------------------------------------------------------
CREATE TABLE metric_reading (
    equipment_id INTEGER          NOT NULL,
    metric_id    SMALLINT         NOT NULL,
    read_at      TIMESTAMPTZ      NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    CONSTRAINT pk_metric_reading        PRIMARY KEY (equipment_id, metric_id, read_at),
    CONSTRAINT fk_reading_equipment     FOREIGN KEY (equipment_id) REFERENCES equipment (id)
                                        ON DELETE CASCADE,
    CONSTRAINT fk_reading_metric        FOREIGN KEY (metric_id) REFERENCES metric (id)
                                        ON DELETE RESTRICT,
    CONSTRAINT ck_reading_value_finite  CHECK (value NOT IN ('NaN', 'Infinity', '-Infinity'))
) PARTITION BY RANGE (read_at);

-- Секції: від трьох місяців до поточного до дванадцяти місяців наперед
-- (назва секції: metric_reading_РРРР_ММ) + секція за замовчуванням.
DO $$
DECLARE
    m_start date := date_trunc('month', now())::date - INTERVAL '3 months';
    m_end   date;
BEGIN
    FOR i IN 0..15 LOOP
        m_end := m_start + INTERVAL '1 month';
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF metric_reading FOR VALUES FROM (%L) TO (%L)',
            'metric_reading_' || to_char(m_start, 'YYYY_MM'), m_start, m_end);
        m_start := m_end;
    END LOOP;
END $$;

CREATE TABLE metric_reading_default PARTITION OF metric_reading DEFAULT;

-- ---------------------------------------------------------------------
-- 6. Інциденти, сповіщення, журнал подій
-- ---------------------------------------------------------------------
CREATE TABLE incident (
    id              BIGINT           GENERATED ALWAYS AS IDENTITY,
    equipment_id    INTEGER          NOT NULL,
    incident_type   incident_kind    NOT NULL,
    threshold_id    INTEGER,
    metric_value    DOUBLE PRECISION,
    status          incident_status  NOT NULL DEFAULT 'new',
    opened_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    escalated_at    TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by INTEGER,
    resolved_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    close_comment   TEXT,
    CONSTRAINT pk_incident            PRIMARY KEY (id),
    CONSTRAINT fk_incident_equipment  FOREIGN KEY (equipment_id) REFERENCES equipment (id)
                                      ON DELETE RESTRICT,
    CONSTRAINT fk_incident_threshold  FOREIGN KEY (threshold_id) REFERENCES threshold (id)
                                      ON DELETE RESTRICT,
    CONSTRAINT fk_incident_ack_user   FOREIGN KEY (acknowledged_by) REFERENCES users (id)
                                      ON DELETE RESTRICT,
    -- інцидент типу 'threshold' завжди посилається на поріг, 'offline' — ніколи
    CONSTRAINT ck_incident_threshold  CHECK ((incident_type = 'threshold') = (threshold_id IS NOT NULL)),
    CONSTRAINT ck_incident_value      CHECK (incident_type = 'offline' OR metric_value IS NOT NULL),
    -- узгодженість статусу з датами життєвого циклу (діаграма станів, ЛР4)
    CONSTRAINT ck_incident_escalated  CHECK (status <> 'escalated' OR escalated_at IS NOT NULL),
    CONSTRAINT ck_incident_ack        CHECK (status IN ('new', 'escalated')
                                             OR (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)),
    CONSTRAINT ck_incident_resolved   CHECK (status NOT IN ('resolved', 'closed') OR resolved_at IS NOT NULL),
    CONSTRAINT ck_incident_closed     CHECK (status <> 'closed'
                                             OR (closed_at IS NOT NULL AND close_comment IS NOT NULL
                                                 AND btrim(close_comment) <> '')),
    CONSTRAINT ck_incident_time_order CHECK (
        (escalated_at    IS NULL OR escalated_at    >= opened_at) AND
        (acknowledged_at IS NULL OR acknowledged_at >= opened_at) AND
        (resolved_at     IS NULL OR resolved_at     >= acknowledged_at) AND
        (closed_at       IS NULL OR closed_at       >= resolved_at))
);

CREATE TABLE notification (
    id            BIGINT         GENERATED ALWAYS AS IDENTITY,
    incident_id   BIGINT         NOT NULL,
    user_id       INTEGER        NOT NULL,
    channel       notify_channel NOT NULL,
    status        notify_status  NOT NULL DEFAULT 'queued',
    is_escalation BOOLEAN        NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    error_message TEXT,
    CONSTRAINT pk_notification          PRIMARY KEY (id),
    CONSTRAINT fk_notification_incident FOREIGN KEY (incident_id) REFERENCES incident (id)
                                        ON DELETE CASCADE,
    CONSTRAINT fk_notification_user     FOREIGN KEY (user_id) REFERENCES users (id)
                                        ON DELETE RESTRICT,
    CONSTRAINT ck_notification_sent     CHECK (status <> 'sent' OR sent_at IS NOT NULL),
    CONSTRAINT ck_notification_failed   CHECK (status <> 'failed' OR error_message IS NOT NULL),
    CONSTRAINT ck_notification_time     CHECK (sent_at IS NULL OR sent_at >= created_at)
);

CREATE TABLE event_log (
    id           BIGINT       GENERATED ALWAYS AS IDENTITY,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    event_type   event_kind   NOT NULL,
    user_id      INTEGER,
    equipment_id INTEGER,
    incident_id  BIGINT,
    message      VARCHAR(500) NOT NULL,
    details      JSONB,
    CONSTRAINT pk_event_log           PRIMARY KEY (id),
    CONSTRAINT fk_event_log_user      FOREIGN KEY (user_id) REFERENCES users (id)
                                      ON DELETE SET NULL,
    CONSTRAINT fk_event_log_equipment FOREIGN KEY (equipment_id) REFERENCES equipment (id)
                                      ON DELETE SET NULL,
    CONSTRAINT fk_event_log_incident  FOREIGN KEY (incident_id) REFERENCES incident (id)
                                      ON DELETE SET NULL,
    CONSTRAINT ck_event_log_details   CHECK (details IS NULL OR jsonb_typeof(details) = 'object')
);

-- ---------------------------------------------------------------------
-- 7. Індекси (PostgreSQL не створює індекси для зовнішніх ключів автоматично)
-- ---------------------------------------------------------------------
CREATE INDEX ix_users_role             ON users (role_id);
CREATE INDEX ix_equipment_type         ON equipment (type_id);
CREATE INDEX ix_equipment_location     ON equipment (location_id);
CREATE INDEX ix_equipment_status       ON equipment (status) WHERE is_active;
CREATE INDEX ix_threshold_metric       ON threshold (metric_id);
CREATE INDEX ix_reading_read_at_brin   ON metric_reading USING BRIN (read_at);
CREATE INDEX ix_incident_equipment     ON incident (equipment_id, opened_at DESC);
CREATE INDEX ix_incident_threshold     ON incident (threshold_id) WHERE threshold_id IS NOT NULL;
CREATE INDEX ix_incident_ack_user      ON incident (acknowledged_by) WHERE acknowledged_by IS NOT NULL;
CREATE INDEX ix_incident_opened        ON incident (opened_at);
CREATE INDEX ix_incident_new           ON incident (opened_at) WHERE status = 'new';
-- не більше одного відкритого інциденту на пару «обладнання — поріг» (або «обладнання — офлайн»)
CREATE UNIQUE INDEX uq_incident_open   ON incident (equipment_id, incident_type, threshold_id) NULLS NOT DISTINCT
                                       WHERE status IN ('new', 'escalated', 'acknowledged');
CREATE INDEX ix_notification_antiflood ON notification (incident_id, user_id, channel, created_at DESC);
CREATE INDEX ix_notification_user      ON notification (user_id);
CREATE INDEX ix_notification_queued    ON notification (created_at) WHERE status = 'queued';
CREATE INDEX ix_event_log_created      ON event_log (created_at DESC);
CREATE INDEX ix_event_log_type         ON event_log (event_type, created_at DESC);
CREATE INDEX ix_event_log_equipment    ON event_log (equipment_id, created_at DESC) WHERE equipment_id IS NOT NULL;
CREATE INDEX ix_event_log_user         ON event_log (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX ix_event_log_incident     ON event_log (incident_id) WHERE incident_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- 8. Коментарі до об'єктів (словник даних у самій БД)
-- ---------------------------------------------------------------------
COMMENT ON SCHEMA netguard              IS 'NetGuard Monitor — моніторинг мережевого обладнання та ДБЖ';
COMMENT ON TABLE  roles                 IS 'Довідник ролей доступу (Сапорт, Адміністратор)';
COMMENT ON TABLE  users                 IS 'Облікові записи користувачів системи';
COMMENT ON TABLE  locations             IS 'Вузли провайдера: ЦОД і районні вузли доступу';
COMMENT ON TABLE  equipment_type        IS 'Довідник типів (моделей) обладнання з профілем SNMP MIB';
COMMENT ON TABLE  metric                IS 'Довідник метрик телеметрії та одиниць виміру';
COMMENT ON TABLE  equipment             IS 'Одиниці мережевого обладнання та ДБЖ, що опитуються за SNMP';
COMMENT ON TABLE  ups_details           IS 'Додаткові паспортні характеристики ДБЖ (підтип equipment, 1:1)';
COMMENT ON TABLE  threshold             IS 'Порогові значення метрик для типу обладнання';
COMMENT ON TABLE  metric_reading        IS 'Історія показників телеметрії (секціонування за місяцями)';
COMMENT ON TABLE  incident              IS 'Інциденти (аварії) та їх життєвий цикл';
COMMENT ON TABLE  notification          IS 'Сповіщення про інциденти за каналами доставки';
COMMENT ON TABLE  event_log             IS 'Журнал подій: зміни стану, інциденти, дії користувачів';
COMMENT ON COLUMN users.password_hash   IS 'Хеш пароля bcrypt (60 символів); відкритий пароль не зберігається';
COMMENT ON COLUMN equipment.failed_polls IS 'Кількість поспіль невдалих опитувань; 3 і більше — статус offline (SR-04.2)';
COMMENT ON COLUMN threshold.min_value   IS 'Нижня межа норми; значення < min_value — порушення';
COMMENT ON COLUMN threshold.max_value   IS 'Верхня межа норми; значення > max_value — порушення';
COMMENT ON COLUMN incident.metric_value IS 'Значення показника, що спричинило інцидент (знімок на момент відкриття)';

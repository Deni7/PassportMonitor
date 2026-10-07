BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

CREATE TABLE providers (
    id VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE services (
    id VARCHAR NOT NULL, 
    name VARCHAR NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE users (
    id BIGINT NOT NULL, 
    chat_id BIGINT NOT NULL, 
    blocked BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE departments (
    id VARCHAR NOT NULL, 
    provider_id VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(provider_id) REFERENCES providers (id)
);

CREATE TABLE locations (
    id VARCHAR NOT NULL, 
    provider_id VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(provider_id) REFERENCES providers (id)
);

CREATE TABLE poll_jobs (
    id VARCHAR NOT NULL, 
    provider_id VARCHAR NOT NULL, 
    next_run TIMESTAMP WITH TIME ZONE NOT NULL, 
    last_success TIMESTAMP WITH TIME ZONE, 
    status VARCHAR NOT NULL, 
    failures INTEGER NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(provider_id) REFERENCES providers (id)
);

CREATE TABLE provider_health (
    id VARCHAR NOT NULL, 
    last_success TIMESTAMP WITH TIME ZONE, 
    last_error VARCHAR, 
    response_time FLOAT NOT NULL, 
    consecutive_failures INTEGER NOT NULL, 
    last_available_slots INTEGER NOT NULL, 
    last_alert TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(id) REFERENCES providers (id)
);

CREATE TABLE provider_services (
    id VARCHAR NOT NULL, 
    provider_id VARCHAR NOT NULL, 
    canonical_service_id VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(canonical_service_id) REFERENCES services (id), 
    FOREIGN KEY(provider_id) REFERENCES providers (id)
);

CREATE TABLE subscriptions (
    id SERIAL NOT NULL, 
    telegram_user_id BIGINT NOT NULL, 
    provider_mode VARCHAR NOT NULL, 
    location_id VARCHAR NOT NULL, 
    location_name VARCHAR NOT NULL, 
    locations JSON NOT NULL, 
    canonical_service_id VARCHAR NOT NULL, 
    date_from DATE NOT NULL, 
    date_to DATE NOT NULL, 
    enabled BOOLEAN NOT NULL, 
    deleted BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(canonical_service_id) REFERENCES services (id), 
    FOREIGN KEY(telegram_user_id) REFERENCES users (id)
);

CREATE INDEX ix_subscriptions_enabled ON subscriptions (enabled);

CREATE INDEX ix_subscriptions_telegram_user_id ON subscriptions (telegram_user_id);

CREATE TABLE slots (
    fingerprint VARCHAR NOT NULL, 
    job_id VARCHAR NOT NULL, 
    state VARCHAR NOT NULL, 
    generation INTEGER NOT NULL, 
    last_seen TIMESTAMP WITH TIME ZONE NOT NULL, 
    disappeared_at TIMESTAMP WITH TIME ZONE, 
    data JSON NOT NULL, 
    PRIMARY KEY (fingerprint), 
    FOREIGN KEY(job_id) REFERENCES poll_jobs (id)
);

CREATE INDEX ix_slots_job_id ON slots (job_id);

CREATE TABLE notifications (
    id SERIAL NOT NULL, 
    telegram_user_id BIGINT NOT NULL, 
    fingerprint VARCHAR NOT NULL, 
    generation INTEGER NOT NULL, 
    job_id VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    attempts INTEGER NOT NULL, 
    next_attempt TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    sent_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(fingerprint) REFERENCES slots (fingerprint), 
    FOREIGN KEY(job_id) REFERENCES poll_jobs (id), 
    FOREIGN KEY(telegram_user_id) REFERENCES users (id), 
    UNIQUE (telegram_user_id, fingerprint, generation)
);

CREATE INDEX ix_notifications_job_id ON notifications (job_id);

CREATE INDEX ix_notifications_status ON notifications (status);

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

-- Running upgrade 0001 -> 0002

CREATE TABLE bot_events (
    id SERIAL NOT NULL, 
    user_id BIGINT, 
    chat_id BIGINT, 
    kind VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    text VARCHAR NOT NULL, 
    data JSON NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    duration_ms FLOAT, 
    PRIMARY KEY (id), 
    FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_bot_events_created_at ON bot_events (created_at);

CREATE INDEX ix_bot_events_kind ON bot_events (kind);

CREATE INDEX ix_bot_events_status ON bot_events (status);

CREATE INDEX ix_bot_events_user_id ON bot_events (user_id);

CREATE TABLE poll_runs (
    id SERIAL NOT NULL, 
    job_id VARCHAR NOT NULL, 
    provider_id VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    duration_ms FLOAT, 
    slot_count INTEGER NOT NULL, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(job_id) REFERENCES poll_jobs (id)
);

CREATE INDEX ix_poll_runs_job_id ON poll_runs (job_id);

CREATE INDEX ix_poll_runs_provider_id ON poll_runs (provider_id);

CREATE INDEX ix_poll_runs_started_at ON poll_runs (started_at);

CREATE INDEX ix_poll_runs_status ON poll_runs (status);

CREATE TABLE poll_run_subscriptions (
    run_id INTEGER NOT NULL, 
    subscription_id INTEGER NOT NULL, 
    user_id BIGINT NOT NULL, 
    PRIMARY KEY (run_id, subscription_id), 
    FOREIGN KEY(run_id) REFERENCES poll_runs (id), 
    FOREIGN KEY(subscription_id) REFERENCES subscriptions (id), 
    FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_poll_run_subscriptions_user_id ON poll_run_subscriptions (user_id);

CREATE TABLE provider_requests (
    id SERIAL NOT NULL, 
    run_id INTEGER NOT NULL, 
    operation VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    duration_ms FLOAT, 
    data JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(run_id) REFERENCES poll_runs (id)
);

CREATE INDEX ix_provider_requests_run_id ON provider_requests (run_id);

CREATE INDEX ix_provider_requests_started_at ON provider_requests (started_at);

CREATE INDEX ix_provider_requests_status ON provider_requests (status);

ALTER TABLE users ADD COLUMN username VARCHAR;

ALTER TABLE users ADD COLUMN full_name VARCHAR;

ALTER TABLE users ADD COLUMN language_code VARCHAR;

ALTER TABLE users ADD COLUMN last_seen TIMESTAMP WITH TIME ZONE;

CREATE INDEX ix_users_last_seen ON users (last_seen);

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

COMMIT;


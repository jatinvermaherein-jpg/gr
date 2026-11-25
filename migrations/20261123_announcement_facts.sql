-- Curated fact layer distilled from the saved announcements (the bot answers from this table).
-- Additive only: the announcement rows remain the raw source of record.
BEGIN;
CREATE TABLE IF NOT EXISTS mech_bot_app.announcement_facts (
    fact_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    guild_name TEXT NOT NULL DEFAULT '',
    message_url TEXT NOT NULL DEFAULT '',
    posted_at BIGINT NOT NULL,
    category TEXT NOT NULL,
    ecr_kind TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'noted',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    entities TEXT NOT NULL DEFAULT '[]',
    event_date_label TEXT NOT NULL DEFAULT '',
    date_basis TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '',
    extractor TEXT NOT NULL DEFAULT 'deterministic',
    model TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS announcement_fact_recent
    ON mech_bot_app.announcement_facts (category, posted_at DESC);
CREATE INDEX IF NOT EXISTS announcement_fact_message
    ON mech_bot_app.announcement_facts (message_id);
REVOKE ALL ON TABLE mech_bot_app.announcement_facts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE mech_bot_app.announcement_facts TO mech_bot_runtime;
COMMIT;

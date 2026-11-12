-- Add only the new private announcement archive; preserve all existing application data.
BEGIN;
CREATE TABLE IF NOT EXISTS mech_bot_app.announcements (
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    guild_name TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    content TEXT NOT NULL,
    message_url TEXT NOT NULL,
    posted_at BIGINT NOT NULL,
    edited_at BIGINT,
    fetched_at BIGINT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS announcement_posted
    ON mech_bot_app.announcements (posted_at DESC);
REVOKE ALL ON TABLE mech_bot_app.announcements FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE mech_bot_app.announcements TO mech_bot_runtime;
COMMIT;

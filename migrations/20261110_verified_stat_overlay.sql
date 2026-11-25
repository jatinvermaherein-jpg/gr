-- Store only the user-confirmed screenshot-stat overlay in the private bot schema.
-- The separate 90 image-level raw OCR records and 147 unrelated public-source
-- documents are not imported. Values remain as displayed in the screenshots.
CREATE TABLE mech_bot_app.stat_rebuild_runs (
    run_id TEXT PRIMARY KEY,
    reviewed_on DATE NOT NULL,
    candidate_rows INTEGER NOT NULL CHECK (candidate_rows = 523),
    screenshot_count INTEGER NOT NULL CHECK (screenshot_count = 84),
    direct_cell_slots_compared INTEGER NOT NULL,
    direct_verified_cell_count INTEGER NOT NULL,
    reconstructed_cell_count INTEGER NOT NULL,
    combined_cell_count INTEGER NOT NULL,
    prior_pinned_direct_cell_count INTEGER NOT NULL,
    historical_direct_count_delta INTEGER NOT NULL,
    candidate_jsonl_sha256 TEXT NOT NULL,
    raw_ocr_manifest_sha256 TEXT NOT NULL,
    combined_overlay_sha256 TEXT NOT NULL,
    manual_manifest_sha256 TEXT NOT NULL,
    correction_log_sha256 TEXT NOT NULL,
    source_archive_sha256 JSONB NOT NULL,
    user_confirmed BOOLEAN NOT NULL,
    user_confirmation_note TEXT NOT NULL,
    pilot_unit_convention TEXT NOT NULL,
    pilot_percent_cells INTEGER NOT NULL,
    raw_ocr_records_imported BOOLEAN NOT NULL DEFAULT FALSE CHECK (raw_ocr_records_imported = FALSE),
    unrelated_public_documents_imported BOOLEAN NOT NULL DEFAULT FALSE CHECK (unrelated_public_documents_imported = FALSE),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (combined_cell_count = direct_verified_cell_count + reconstructed_cell_count),
    CHECK (historical_direct_count_delta = direct_verified_cell_count - prior_pinned_direct_cell_count)
);

CREATE TABLE mech_bot_app.stat_rebuild_sources (
    rebuild_id TEXT NOT NULL REFERENCES mech_bot_app.stat_rebuild_runs(run_id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    archive_name TEXT NOT NULL CHECK (archive_name IN ('PILOT.zip', 'MA SS.zip')),
    source_path TEXT NOT NULL,
    source_screenshot_sha256 TEXT NOT NULL,
    source_archive_sha256 TEXT NOT NULL,
    candidate_rows INTEGER NOT NULL,
    direct_rows_visually_reviewed INTEGER NOT NULL,
    direct_cell_slots_compared INTEGER NOT NULL,
    wrapped_reconstruction_id TEXT,
    review_notes TEXT NOT NULL,
    reviewed_on DATE NOT NULL,
    PRIMARY KEY (rebuild_id, card_id),
    UNIQUE (rebuild_id, source_path)
);

CREATE TABLE mech_bot_app.stat_rebuild_rows (
    rebuild_id TEXT NOT NULL REFERENCES mech_bot_app.stat_rebuild_runs(run_id) ON DELETE CASCADE,
    row_key TEXT NOT NULL,
    card_id TEXT NOT NULL,
    archive_name TEXT NOT NULL CHECK (archive_name IN ('PILOT.zip', 'MA SS.zip')),
    entity_title TEXT,
    stat_label TEXT,
    row_type TEXT NOT NULL CHECK (row_type IN ('direct', 'reconstructed')),
    candidate_row_id TEXT,
    reconstruction_id TEXT,
    baseline_y NUMERIC,
    description TEXT,
    source_fragment_candidate_row_ids JSONB,
    reviewed_on DATE NOT NULL,
    PRIMARY KEY (rebuild_id, row_key),
    FOREIGN KEY (rebuild_id, card_id)
        REFERENCES mech_bot_app.stat_rebuild_sources(rebuild_id, card_id) ON DELETE CASCADE,
    CHECK (
        (row_type = 'direct' AND candidate_row_id IS NOT NULL AND reconstruction_id IS NULL)
        OR
        (row_type = 'reconstructed' AND reconstruction_id IS NOT NULL)
    )
);

CREATE TABLE mech_bot_app.verified_stat_cells (
    rebuild_id TEXT NOT NULL,
    cell_key TEXT NOT NULL,
    row_key TEXT NOT NULL,
    rank_column SMALLINT NOT NULL CHECK (rank_column BETWEEN 1 AND 7),
    value TEXT NOT NULL,
    value_unit TEXT NOT NULL CHECK (value_unit IN ('percentage', 'as_displayed')),
    cell_status TEXT NOT NULL,
    user_confirmed BOOLEAN NOT NULL DEFAULT TRUE,
    reviewed_on DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rebuild_id, cell_key),
    FOREIGN KEY (rebuild_id, row_key)
        REFERENCES mech_bot_app.stat_rebuild_rows(rebuild_id, row_key) ON DELETE CASCADE
);
CREATE UNIQUE INDEX verified_stat_cells_row_rank
    ON mech_bot_app.verified_stat_cells (rebuild_id, row_key, rank_column);
CREATE INDEX verified_stat_cells_rank
    ON mech_bot_app.verified_stat_cells (rebuild_id, rank_column);

REVOKE ALL ON TABLE mech_bot_app.stat_rebuild_runs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE mech_bot_app.stat_rebuild_sources FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE mech_bot_app.stat_rebuild_rows FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE mech_bot_app.verified_stat_cells FROM PUBLIC, anon, authenticated;
GRANT SELECT ON TABLE mech_bot_app.stat_rebuild_runs TO mech_bot_runtime;
GRANT SELECT ON TABLE mech_bot_app.stat_rebuild_sources TO mech_bot_runtime;
GRANT SELECT ON TABLE mech_bot_app.stat_rebuild_rows TO mech_bot_runtime;
GRANT SELECT ON TABLE mech_bot_app.verified_stat_cells TO mech_bot_runtime;

-- Recorder hardening: coverage ledger for audio/video capture and uploads.
-- Apply on destroyer_db before restarting stream-daemon/video-segment-uploader.

CREATE TABLE IF NOT EXISTS recording_coverage (
    id BIGSERIAL PRIMARY KEY,
    stream_id TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('audio', 'video')),
    period_start_utc TIMESTAMPTZ NOT NULL,
    period_end_utc TIMESTAMPTZ NOT NULL,
    expected_seconds INTEGER NOT NULL,
    actual_seconds DOUBLE PRECISION,
    local_path TEXT,
    s3_key TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'validated',
            'uploaded',
            'upload_failed',
            'invalid',
            'skipped'
        )
    ),
    reason TEXT,
    size_bytes BIGINT,
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    source_service TEXT NOT NULL,
    pipeline_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_recording_coverage_period
ON recording_coverage (stream_id, media_type, period_start_utc);

CREATE INDEX IF NOT EXISTS ix_recording_coverage_status
ON recording_coverage (status, updated_at);

CREATE INDEX IF NOT EXISTS ix_recording_coverage_stream_time
ON recording_coverage (stream_id, period_start_utc DESC);


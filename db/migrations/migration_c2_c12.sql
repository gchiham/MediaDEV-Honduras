BEGIN;

-- C2: media_sources
CREATE TABLE media_sources (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_catalog_id TEXT        REFERENCES stream_catalog(id),
    name              TEXT        NOT NULL,
    media_type        TEXT        NOT NULL CHECK (media_type IN ('radio', 'tv')),
    country           TEXT        NOT NULL DEFAULT 'HN',
    region            TEXT,
    city              TEXT,
    frequency_channel TEXT,
    description       TEXT,
    logo_url          TEXT,
    lifecycle_status  TEXT        NOT NULL DEFAULT 'active'
                          CHECK (lifecycle_status IN ('active', 'discontinued')),
    discontinued_at   TIMESTAMPTZ,
    health_status     TEXT        NOT NULL DEFAULT 'healthy'
                          CHECK (health_status IN ('healthy', 'degraded', 'offline')),
    health_updated_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_media_sources_lifecycle
    ON media_sources (lifecycle_status) WHERE lifecycle_status = 'active';
CREATE UNIQUE INDEX idx_media_sources_stream_catalog
    ON media_sources (stream_catalog_id) WHERE stream_catalog_id IS NOT NULL;

-- C3: capture_config
CREATE TABLE capture_config (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    media_source_id UUID        NOT NULL UNIQUE REFERENCES media_sources(id) ON DELETE CASCADE,
    stream_url      TEXT        NOT NULL,
    route           TEXT        NOT NULL DEFAULT 'socks5'
                        CHECK (route IN ('socks5', 'direct')),
    gateway_id      INTEGER     REFERENCES gateways(id),
    hls_path        TEXT,
    mp3_s3_prefix   TEXT,
    ts_s3_prefix    TEXT,
    ffmpeg_extra    TEXT,
    is_enabled      BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_socks5_requires_gateway
        CHECK (route <> 'socks5' OR gateway_id IS NOT NULL)
);

-- C4: tenant_media_source_assignments
CREATE TABLE tenant_media_source_assignments (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    media_source_id       UUID        NOT NULL REFERENCES media_sources(id),
    assigned_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by_user_id   UUID,
    unassigned_at         TIMESTAMPTZ,
    unassigned_by_user_id UUID,
    is_active             BOOLEAN     NOT NULL DEFAULT true,
    UNIQUE (tenant_id, media_source_id)
);
CREATE INDEX idx_tmsa_tenant_active ON tenant_media_source_assignments (tenant_id) WHERE is_active;
CREATE INDEX idx_tmsa_lookup        ON tenant_media_source_assignments (tenant_id, media_source_id);

-- C5: platform_staff
CREATE TABLE platform_staff (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT        NOT NULL UNIQUE,
    name          TEXT        NOT NULL,
    role          TEXT        NOT NULL CHECK (role IN ('super_admin', 'support')),
    password_hash TEXT        NOT NULL,
    is_active     BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- C6: impersonation_sessions
CREATE TABLE impersonation_sessions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    staff_id     UUID        NOT NULL REFERENCES platform_staff(id),
    tenant_id    UUID        NOT NULL REFERENCES tenants(id),
    reason       TEXT        NOT NULL,
    is_read_only BOOLEAN     NOT NULL DEFAULT true,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,
    ip_address   INET,
    CONSTRAINT chk_session_window CHECK (ended_at IS NULL OR ended_at > started_at)
);
CREATE INDEX idx_impersonation_tenant ON impersonation_sessions (tenant_id, started_at DESC);
CREATE INDEX idx_impersonation_active ON impersonation_sessions (staff_id) WHERE ended_at IS NULL;

-- C7: campaign_media_sources
CREATE TABLE campaign_media_sources (
    campaign_id      UUID        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    media_source_id  UUID        NOT NULL REFERENCES media_sources(id),
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by_user_id UUID,
    PRIMARY KEY (campaign_id, media_source_id)
);
CREATE INDEX idx_cms_media_source ON campaign_media_sources (media_source_id);

-- C8: media_source_requests
CREATE TABLE media_source_requests (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID        NOT NULL REFERENCES tenants(id),
    requested_by_user_id     UUID,
    signal_name              TEXT        NOT NULL,
    media_type               TEXT        CHECK (media_type IN ('radio', 'tv')),
    country                  TEXT,
    city                     TEXT,
    frequency_channel        TEXT,
    stream_url_hint          TEXT,
    notes                    TEXT,
    status                   TEXT        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by_staff_id     UUID        REFERENCES platform_staff(id),
    reviewed_at              TIMESTAMPTZ,
    review_notes             TEXT,
    resolved_media_source_id UUID        REFERENCES media_sources(id),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_review_complete CHECK (status = 'pending' OR reviewed_by_staff_id IS NOT NULL)
);
CREATE INDEX idx_msr_tenant  ON media_source_requests (tenant_id, status);
CREATE INDEX idx_msr_pending ON media_source_requests (status) WHERE status = 'pending';

-- C9: ALTER tenants (plan_id ya existe, IF NOT EXISTS lo salta)
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS plan_id            UUID        REFERENCES plans(id),
    ADD COLUMN IF NOT EXISTS billing_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS billing_status     TEXT        NOT NULL DEFAULT 'active'
        CHECK (billing_status IN ('active', 'suspended', 'cancelled'));

UPDATE tenants
   SET plan_id = (SELECT id FROM plans WHERE name = 'professional' LIMIT 1)
 WHERE plan_id IS NULL;

-- C10: ALTER users (role, is_active ya existen, IF NOT EXISTS los salta)
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS role                 TEXT    NOT NULL DEFAULT 'admin'
        CHECK (role IN ('admin', 'viewer')),
    ADD COLUMN IF NOT EXISTS can_generate_reports BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS can_share_evidence   BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS can_export_data      BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS is_active            BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS last_login_at        TIMESTAMPTZ;

-- FKs diferidas C4
ALTER TABLE tenant_media_source_assignments
    ADD CONSTRAINT fk_tmsa_assigned_by   FOREIGN KEY (assigned_by_user_id)   REFERENCES users(id),
    ADD CONSTRAINT fk_tmsa_unassigned_by FOREIGN KEY (unassigned_by_user_id) REFERENCES users(id);

-- FK diferida C7
ALTER TABLE campaign_media_sources
    ADD CONSTRAINT fk_cms_added_by FOREIGN KEY (added_by_user_id) REFERENCES users(id);

-- FK diferida C8
ALTER TABLE media_source_requests
    ADD CONSTRAINT fk_msr_requested_by FOREIGN KEY (requested_by_user_id) REFERENCES users(id);

-- C12: ALTER advertisements
ALTER TABLE advertisements
    ADD CONSTRAINT chk_clip_pad_range
        CHECK (clip_pad_seconds IS NULL OR (clip_pad_seconds >= 0 AND clip_pad_seconds <= 20));

ALTER TABLE advertisements
    ALTER COLUMN clip_pad_seconds SET DEFAULT 5;

ALTER TABLE advertisements
    ADD COLUMN IF NOT EXISTS client_id UUID REFERENCES clients(id);

UPDATE advertisements a
   SET client_id = c.client_id
  FROM campaigns c
 WHERE a.campaign_id = c.id
   AND a.client_id IS NULL;

-- fingerprint_detections: media_source_id (consecuencia de C2)
ALTER TABLE fingerprint_detections
    ADD COLUMN IF NOT EXISTS media_source_id UUID REFERENCES media_sources(id);

COMMIT;

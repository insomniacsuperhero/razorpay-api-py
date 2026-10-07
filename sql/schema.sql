-- Proof Bridge schema for Supabase (generated from Alembic). Paste into Supabase SQL editor, or run 'alembic upgrade head'.
BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> ade886fd8376

CREATE TABLE dispute_runs (
    dispute_id TEXT NOT NULL, 
    payment_id TEXT NOT NULL, 
    merchant_id TEXT NOT NULL, 
    draft_summary TEXT, 
    summary_edited BOOLEAN NOT NULL, 
    contested_amount BIGINT, 
    timer_started_at TIMESTAMP WITH TIME ZONE, 
    submitted_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (dispute_id)
);

CREATE TABLE idempotency_keys (
    key TEXT NOT NULL, 
    request_hash TEXT NOT NULL, 
    status_code INTEGER NOT NULL, 
    response JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (key)
);

CREATE TABLE link_tokens (
    jti UUID NOT NULL, 
    dispute_id TEXT NOT NULL, 
    merchant_id TEXT NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    exchanged_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (jti)
);

CREATE TABLE metric_events (
    id BIGSERIAL NOT NULL, 
    dispute_id TEXT, 
    evidence_id UUID, 
    event_name TEXT NOT NULL, 
    metadata JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE TABLE webhook_events (
    id BIGSERIAL NOT NULL, 
    source TEXT NOT NULL, 
    event_key TEXT NOT NULL, 
    event_type TEXT NOT NULL, 
    payload_hash TEXT, 
    processed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (event_key)
);

CREATE TABLE upload_sessions (
    id UUID NOT NULL, 
    dispute_id TEXT NOT NULL, 
    object_key TEXT NOT NULL, 
    original_filename TEXT NOT NULL, 
    declared_mime_type TEXT NOT NULL, 
    size_bytes BIGINT NOT NULL, 
    status VARCHAR(32) NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(dispute_id) REFERENCES dispute_runs (dispute_id) ON DELETE CASCADE, 
    UNIQUE (object_key)
);

CREATE TABLE evidence_items (
    id UUID NOT NULL, 
    dispute_id TEXT NOT NULL, 
    upload_id UUID NOT NULL, 
    original_filename TEXT NOT NULL, 
    original_object_key TEXT NOT NULL, 
    normalized_object_key TEXT, 
    detected_mime_type TEXT, 
    document_type TEXT, 
    status VARCHAR(32) NOT NULL, 
    error_code VARCHAR(64), 
    suggested_field TEXT, 
    final_field TEXT, 
    verdict VARCHAR(32), 
    verdict_reason TEXT, 
    question TEXT, 
    readable BOOLEAN NOT NULL, 
    merchant_confirmed BOOLEAN NOT NULL, 
    bank_flag TEXT, 
    processing_notes JSONB NOT NULL, 
    quality_flags JSONB NOT NULL, 
    mismatches JSONB NOT NULL, 
    original_facts JSONB NOT NULL, 
    final_facts JSONB NOT NULL, 
    razorpay_document_id TEXT, 
    correction_count INTEGER NOT NULL, 
    removed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(dispute_id) REFERENCES dispute_runs (dispute_id) ON DELETE CASCADE, 
    FOREIGN KEY(upload_id) REFERENCES upload_sessions (id)
);

CREATE TABLE merchant_edits (
    id BIGSERIAL NOT NULL, 
    evidence_id UUID NOT NULL, 
    fact_key TEXT NOT NULL, 
    old_value JSONB, 
    new_value JSONB, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(evidence_id) REFERENCES evidence_items (id) ON DELETE CASCADE
);

INSERT INTO alembic_version (version_num) VALUES ('ade886fd8376') RETURNING alembic_version.version_num;

COMMIT;


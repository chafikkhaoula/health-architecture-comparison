BEGIN;

CREATE TABLE clinical_records (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT clinical_records_pk
        PRIMARY KEY (resource_type, resource_id),

    CONSTRAINT clinical_records_resource_type_ck
        CHECK (
            resource_type IN (
                'Patient',
                'Observation',
                'Condition',
                'DiagnosticReport'
            )
        ),

    CONSTRAINT clinical_records_resource_id_ck
        CHECK (
            char_length(resource_id) BETWEEN 1 AND 64
            AND resource_id ~ '^[A-Za-z0-9.-]+$'
        ),

    CONSTRAINT clinical_records_payload_object_ck
        CHECK (jsonb_typeof(payload) = 'object'),

    CONSTRAINT clinical_records_payload_type_ck
        CHECK (
            payload ->> 'resourceType' IS NOT NULL
            AND payload ->> 'resourceType' = resource_type
        ),

    CONSTRAINT clinical_records_payload_id_ck
        CHECK (
            payload ->> 'id' IS NOT NULL
            AND payload ->> 'id' = resource_id
        ),

    CONSTRAINT clinical_records_payload_hash_ck
        CHECK (
            char_length(payload_hash) = 64
            AND payload_hash ~ '^[0-9a-f]{64}$'
        ),

    CONSTRAINT clinical_records_timestamp_ck
        CHECK (updated_at >= created_at)
);

CREATE TABLE authorization_rules (
    rule_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    principal_actor_id TEXT NOT NULL,
    principal_organization_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT authorization_rules_rule_id_ck
        CHECK (
            char_length(rule_id) BETWEEN 1 AND 128
            AND rule_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT authorization_rules_actor_id_ck
        CHECK (
            char_length(principal_actor_id) BETWEEN 1 AND 128
            AND principal_actor_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT authorization_rules_organization_id_ck
        CHECK (
            char_length(principal_organization_id) BETWEEN 1 AND 128
            AND principal_organization_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT authorization_rules_decision_ck
        CHECK (decision IN ('ALLOW', 'DENY')),

    CONSTRAINT authorization_rules_timestamp_ck
        CHECK (updated_at >= created_at),

    CONSTRAINT authorization_rules_record_fk
        FOREIGN KEY (resource_type, resource_id)
        REFERENCES clinical_records (resource_type, resource_id)
        ON DELETE RESTRICT,

    CONSTRAINT authorization_rules_principal_unique
        UNIQUE (
            resource_type,
            resource_id,
            principal_actor_id,
            principal_organization_id
        )
);

CREATE TABLE audit_events (
    sequence BIGINT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT,
    event_timestamp TIMESTAMPTZ NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,

    CONSTRAINT audit_events_sequence_ck
        CHECK (sequence >= 1),

    CONSTRAINT audit_events_event_id_ck
        CHECK (
            char_length(event_id) BETWEEN 1 AND 128
            AND event_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT audit_events_actor_id_ck
        CHECK (
            char_length(actor_id) BETWEEN 1 AND 128
            AND actor_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT audit_events_organization_id_ck
        CHECK (
            char_length(organization_id) BETWEEN 1 AND 128
            AND organization_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$'
        ),

    CONSTRAINT audit_events_action_ck
        CHECK (action IN ('OP1', 'OP3', 'OP4')),

    CONSTRAINT audit_events_decision_ck
        CHECK (
            (
                action = 'OP4'
                AND decision IN ('ALLOW', 'DENY')
            )
            OR
            (
                action IN ('OP1', 'OP3')
                AND decision IS NULL
            )
        ),

    CONSTRAINT audit_events_previous_hash_ck
        CHECK (
            previous_hash IS NULL
            OR (
                char_length(previous_hash) = 64
                AND previous_hash ~ '^[0-9a-f]{64}$'
            )
        ),

    CONSTRAINT audit_events_event_hash_ck
        CHECK (
            char_length(event_hash) = 64
            AND event_hash ~ '^[0-9a-f]{64}$'
        ),

    CONSTRAINT audit_events_record_fk
        FOREIGN KEY (resource_type, resource_id)
        REFERENCES clinical_records (resource_type, resource_id)
        ON DELETE RESTRICT
);

CREATE INDEX audit_events_record_sequence_idx
    ON audit_events (resource_type, resource_id, sequence);

COMMIT;

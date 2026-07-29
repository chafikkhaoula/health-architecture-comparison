BEGIN;

CREATE TABLE clinical_payloads (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT clinical_payloads_pk
        PRIMARY KEY (resource_type, resource_id),

    CONSTRAINT clinical_payloads_resource_type_ck
        CHECK (
            resource_type IN (
                'Patient',
                'Observation',
                'Condition',
                'DiagnosticReport'
            )
        ),

    CONSTRAINT clinical_payloads_resource_id_ck
        CHECK (
            char_length(resource_id) BETWEEN 1 AND 64
            AND resource_id ~ '^[A-Za-z0-9.-]+$'
        ),

    CONSTRAINT clinical_payloads_payload_object_ck
        CHECK (jsonb_typeof(payload) = 'object'),

    CONSTRAINT clinical_payloads_payload_type_ck
        CHECK (
            payload ->> 'resourceType' IS NOT NULL
            AND payload ->> 'resourceType' = resource_type
        ),

    CONSTRAINT clinical_payloads_payload_id_ck
        CHECK (
            payload ->> 'id' IS NOT NULL
            AND payload ->> 'id' = resource_id
        )
);

COMMIT;

from datetime import datetime

import pytest
from pydantic import ValidationError

from shared.schemas.models import (
    FHIRResourceEnvelope,
    IntegrityEvidence,
    ResourceType,
    ResourceType,
)


def patient_payload() -> dict[str, object]:
    return {
        "resourceType": "Patient",
        "id": "patient-000001",
        "active": True,
    }


@pytest.mark.parametrize(
    ("resource_type", "resource_id"),
    [
        ("Patient", "patient-000001"),
        ("Observation", "observation-000001"),
        ("Condition", "condition-000001"),
        ("DiagnosticReport", "report-000001"),
    ],
)
def test_fhir_resource_accepts_supported_payload(
    resource_type: str,
    resource_id: str,
) -> None:
    payload = {
        "resourceType": resource_type,
        "id": resource_id,
    }

    resource = FHIRResourceEnvelope.from_payload(payload)

    assert resource.resource_type.value == resource_type
    assert resource.resource_id == resource_id
    assert resource.payload == payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resourceType", "Encounter"),
        ("resourceType", 123),
        ("id", None),
    ],
)
def test_fhir_resource_rejects_unsupported_or_missing_identity(
    field: str,
    value: object,
) -> None:
    payload = patient_payload()
    payload[field] = value

    with pytest.raises(ValueError):
        FHIRResourceEnvelope.from_payload(payload)


def test_fhir_resource_rejects_identity_mismatch() -> None:
    with pytest.raises(ValidationError):
        FHIRResourceEnvelope(
            resource_type=ResourceType.PATIENT,
            resource_id="patient-000002",
            payload=patient_payload(),
        )


def test_fhir_resource_rejects_invalid_fhir_id() -> None:
    payload = patient_payload()
    payload["id"] = "patient/000001"

    with pytest.raises(ValidationError):
        FHIRResourceEnvelope.from_payload(payload)


def test_fhir_resource_rejects_non_json_payload_values() -> None:
    payload = patient_payload()
    payload["generated_at"] = datetime(2026, 1, 1)

    with pytest.raises(ValidationError):
        FHIRResourceEnvelope.from_payload(payload)


def test_strict_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        IntegrityEvidence(
            payload_hash="0" * 64,
            unexpected="not allowed",
        )


def test_integrity_evidence_rejects_malformed_digest() -> None:
    with pytest.raises(ValidationError):
        IntegrityEvidence(payload_hash="ABC")

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from random import Random

from pydantic import JsonValue

from shared.hashing import payload_sha256
from shared.schemas import FHIRResourceEnvelope

DEFAULT_SEED = 42

_REFERENCE_TIME = datetime(
    2026,
    1,
    1,
    8,
    0,
    0,
    tzinfo=timezone.utc,
)
_BIRTH_DATE_START = date(1940, 1, 1)
_BIRTH_DATE_END = date(2005, 12, 31)

_GIVEN_NAMES = (
    "Amal",
    "Imane",
    "Sara",
    "Yasmine",
    "Omar",
    "Adam",
    "Mehdi",
    "Youssef",
)

_FAMILY_NAMES = (
    "Alaoui",
    "Bennani",
    "El Idrissi",
    "Fassi",
    "Mansouri",
    "Naciri",
)

_CITIES = (
    "Casablanca",
    "El Jadida",
    "Fes",
    "Marrakech",
    "Rabat",
    "Tangier",
)

_CONDITIONS = (
    ("38341003", "Hypertensive disorder"),
    ("44054006", "Type 2 diabetes mellitus"),
    ("195967001", "Asthma"),
    ("55822004", "Hyperlipidemia"),
)


def _fhir_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _codeable_concept(
    system: str,
    code: str,
    display: str,
) -> dict[str, JsonValue]:
    return {
        "coding": [
            {
                "system": system,
                "code": code,
                "display": display,
            }
        ],
        "text": display,
    }


def _birth_date(rng: Random) -> str:
    span_days = (_BIRTH_DATE_END - _BIRTH_DATE_START).days
    offset = rng.randint(0, span_days)
    return (_BIRTH_DATE_START + timedelta(days=offset)).isoformat()


def _build_case(
    index: int,
    rng: Random,
) -> tuple[FHIRResourceEnvelope, ...]:
    suffix = f"{index:06d}"

    patient_id = f"patient-{suffix}"
    observation_id = f"observation-{suffix}"
    condition_id = f"condition-{suffix}"
    report_id = f"report-{suffix}"

    patient_reference = f"Patient/{patient_id}"
    observation_reference = f"Observation/{observation_id}"

    clinical_time = _REFERENCE_TIME + timedelta(
        days=index - 1,
        minutes=rng.randrange(0, 480),
    )

    condition_code, condition_display = rng.choice(_CONDITIONS)

    patient: dict[str, JsonValue] = {
        "resourceType": "Patient",
        "id": patient_id,
        "active": True,
        "identifier": [
            {
                "system": "https://example.org/fhir/identifier/synthetic",
                "value": f"SYN-{suffix}",
            }
        ],
        "name": [
            {
                "use": "official",
                "family": rng.choice(_FAMILY_NAMES),
                "given": [rng.choice(_GIVEN_NAMES)],
            }
        ],
        "gender": rng.choice(("female", "male")),
        "birthDate": _birth_date(rng),
        "address": [
            {
                "use": "home",
                "city": rng.choice(_CITIES),
                "country": "MA",
            }
        ],
    }

    observation: dict[str, JsonValue] = {
        "resourceType": "Observation",
        "id": observation_id,
        "status": "final",
        "category": [
            _codeable_concept(
                "http://terminology.hl7.org/CodeSystem/"
                "observation-category",
                "laboratory",
                "Laboratory",
            )
        ],
        "code": _codeable_concept(
            "http://loinc.org",
            "718-7",
            "Hemoglobin [Mass/volume] in Blood",
        ),
        "subject": {
            "reference": patient_reference,
        },
        "effectiveDateTime": _fhir_datetime(clinical_time),
        "valueQuantity": {
            "value": round(rng.uniform(11.0, 17.0), 1),
            "unit": "g/dL",
            "system": "http://unitsofmeasure.org",
            "code": "g/dL",
        },
    }

    condition: dict[str, JsonValue] = {
        "resourceType": "Condition",
        "id": condition_id,
        "clinicalStatus": _codeable_concept(
            "http://terminology.hl7.org/CodeSystem/"
            "condition-clinical",
            "active",
            "Active",
        ),
        "verificationStatus": _codeable_concept(
            "http://terminology.hl7.org/CodeSystem/"
            "condition-ver-status",
            "confirmed",
            "Confirmed",
        ),
        "category": [
            _codeable_concept(
                "http://terminology.hl7.org/CodeSystem/"
                "condition-category",
                "problem-list-item",
                "Problem List Item",
            )
        ],
        "code": _codeable_concept(
            "http://snomed.info/sct",
            condition_code,
            condition_display,
        ),
        "subject": {
            "reference": patient_reference,
        },
        "onsetDateTime": _fhir_datetime(
            clinical_time - timedelta(days=rng.randint(30, 720))
        ),
        "recordedDate": _fhir_datetime(clinical_time),
    }

    diagnostic_report: dict[str, JsonValue] = {
        "resourceType": "DiagnosticReport",
        "id": report_id,
        "status": "final",
        "category": [
            _codeable_concept(
                "http://terminology.hl7.org/CodeSystem/v2-0074",
                "LAB",
                "Laboratory",
            )
        ],
        "code": _codeable_concept(
            "http://loinc.org",
            "58410-2",
            "Complete blood count panel",
        ),
        "subject": {
            "reference": patient_reference,
        },
        "effectiveDateTime": _fhir_datetime(clinical_time),
        "issued": _fhir_datetime(
            clinical_time + timedelta(hours=1)
        ),
        "result": [
            {
                "reference": observation_reference,
            }
        ],
    }

    return tuple(
        FHIRResourceEnvelope.from_payload(payload)
        for payload in (
            patient,
            observation,
            condition,
            diagnostic_report,
        )
    )


def generate_fhir_resources(
    patient_count: int,
    seed: int = DEFAULT_SEED,
) -> tuple[FHIRResourceEnvelope, ...]:
    """Generate deterministic linked synthetic FHIR R4 resources."""
    if type(patient_count) is not int or patient_count < 1:
        raise ValueError("patient_count must be a positive integer")

    if type(seed) is not int:
        raise ValueError("seed must be an integer")

    rng = Random(seed)
    resources: list[FHIRResourceEnvelope] = []

    for index in range(1, patient_count + 1):
        resources.extend(_build_case(index, rng))

    return tuple(resources)


def dataset_sha256(
    resources: Sequence[FHIRResourceEnvelope],
) -> str:
    """Hash an ordered resource dataset using the shared implementation."""
    return payload_sha256(
        [resource.payload for resource in resources]
    )

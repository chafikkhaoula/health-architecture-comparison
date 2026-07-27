from collections import Counter

import pytest

from shared.data_generator import (
    DEFAULT_SEED,
    dataset_sha256,
    generate_fhir_resources,
)
from shared.hashing import payload_sha256
from shared.schemas import ResourceType


def resource_identity(resource: object) -> tuple[object, object]:
    return (
        getattr(resource, "resource_type"),
        getattr(resource, "resource_id"),
    )


def test_generator_is_reproducible_for_same_seed() -> None:
    first = generate_fhir_resources(5, seed=DEFAULT_SEED)
    second = generate_fhir_resources(5, seed=DEFAULT_SEED)

    assert first == second
    assert dataset_sha256(first) == dataset_sha256(second)


def test_seed_changes_data_but_not_identifiers() -> None:
    first = generate_fhir_resources(5, seed=42)
    second = generate_fhir_resources(5, seed=43)

    assert [resource_identity(item) for item in first] == [
        resource_identity(item) for item in second
    ]
    assert dataset_sha256(first) != dataset_sha256(second)


@pytest.mark.parametrize("patient_count", [1, 3])
def test_generator_returns_four_resources_per_patient(
    patient_count: int,
) -> None:
    resources = generate_fhir_resources(patient_count)

    assert len(resources) == patient_count * 4

    counts = Counter(
        resource.resource_type for resource in resources
    )

    assert counts == {
        ResourceType.PATIENT: patient_count,
        ResourceType.OBSERVATION: patient_count,
        ResourceType.CONDITION: patient_count,
        ResourceType.DIAGNOSTIC_REPORT: patient_count,
    }

    expected_order = (
        ResourceType.PATIENT,
        ResourceType.OBSERVATION,
        ResourceType.CONDITION,
        ResourceType.DIAGNOSTIC_REPORT,
    )

    for offset in range(0, len(resources), 4):
        assert tuple(
            resource.resource_type
            for resource in resources[offset : offset + 4]
        ) == expected_order


def test_resource_identifiers_are_unique() -> None:
    resources = generate_fhir_resources(20)

    identities = {
        (resource.resource_type, resource.resource_id)
        for resource in resources
    }

    assert len(identities) == len(resources)


def test_subject_and_result_references_are_resolvable() -> None:
    resources = generate_fhir_resources(10)

    resources_by_reference = {
        f"{resource.resource_type.value}/{resource.resource_id}": resource
        for resource in resources
    }

    for resource in resources:
        if resource.resource_type is ResourceType.PATIENT:
            continue

        subject = resource.payload["subject"]
        assert isinstance(subject, dict)

        subject_reference = subject["reference"]
        assert isinstance(subject_reference, str)
        assert subject_reference in resources_by_reference
        assert subject_reference.startswith("Patient/")

        if resource.resource_type is ResourceType.DIAGNOSTIC_REPORT:
            results = resource.payload["result"]
            assert isinstance(results, list)
            assert len(results) == 1

            result = results[0]
            assert isinstance(result, dict)

            result_reference = result["reference"]
            assert isinstance(result_reference, str)
            assert result_reference in resources_by_reference
            assert result_reference.startswith("Observation/")


def test_generated_payloads_have_valid_hashes() -> None:
    resources = generate_fhir_resources(10)

    hashes = [
        payload_sha256(resource.payload)
        for resource in resources
    ]

    assert len(hashes) == 40
    assert all(len(digest) == 64 for digest in hashes)
    assert len(set(hashes)) == len(hashes)
    assert len(dataset_sha256(resources)) == 64


@pytest.mark.parametrize(
    "invalid_count",
    [0, -1, True, 1.5],
)
def test_generator_rejects_invalid_patient_count(
    invalid_count: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="patient_count must be a positive integer",
    ):
        generate_fhir_resources(invalid_count)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_seed",
    [True, "42"],
)
def test_generator_rejects_invalid_seed(
    invalid_seed: object,
) -> None:
    with pytest.raises(ValueError, match="seed must be an integer"):
        generate_fhir_resources(
            1,
            seed=invalid_seed,  # type: ignore[arg-type]
        )


def test_reference_dataset_hash_is_stable() -> None:
    resources = generate_fhir_resources(3, seed=DEFAULT_SEED)

    assert dataset_sha256(resources) == (
        "33dab9d75b492ca1c267fc838f55b892"
        "19afd0cac305b709f908cce587992476"
    )

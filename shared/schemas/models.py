from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    StringConstraints,
    model_validator,
)

FHIRId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9\-.]+$",
    ),
]

Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


class ResourceType(StrEnum):
    PATIENT = "Patient"
    OBSERVATION = "Observation"
    CONDITION = "Condition"
    DIAGNOSTIC_REPORT = "DiagnosticReport"


class OperationId(StrEnum):
    CREATE_RECORD = "OP1"
    RETRIEVE_RECORD = "OP2"
    UPDATE_AUTHORIZATION = "OP3"
    EVALUATE_ACCESS = "OP4"
    VERIFY_INTEGRITY = "OP5"
    RETRIEVE_AUDIT = "OP6"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )


class FHIRResourceEnvelope(StrictModel):
    resource_type: ResourceType
    resource_id: FHIRId
    payload: dict[str, JsonValue]

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> Self:
        raw_type = payload.get("resourceType")
        raw_id = payload.get("id")

        if not isinstance(raw_type, str):
            raise ValueError("FHIR payload requires a string resourceType")

        if not isinstance(raw_id, str):
            raise ValueError("FHIR payload requires a string id")

        try:
            resource_type = ResourceType(raw_type)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported FHIR resourceType: {raw_type}"
            ) from exc

        return cls(
            resource_type=resource_type,
            resource_id=raw_id,
            payload=payload,
        )

    @model_validator(mode="after")
    def validate_payload_identity(self) -> Self:
        if self.payload.get("resourceType") != self.resource_type.value:
            raise ValueError(
                "payload resourceType does not match resource_type"
            )

        if self.payload.get("id") != self.resource_id:
            raise ValueError("payload id does not match resource_id")

        return self


class IntegrityEvidence(StrictModel):
    algorithm: Literal["sha256"] = "sha256"
    canonicalization: Literal["RFC8785"] = "RFC8785"
    payload_hash: Sha256Digest

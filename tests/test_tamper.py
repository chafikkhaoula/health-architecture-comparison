from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from benchmark.tamper import (
    SCENARIOS,
    TamperTrial,
    _first_invalid_rows,
    _protected_write_outcome_met,
    _record_id,
    _summaries,
)
from shared.hashing.audit import audit_event_sha256
from shared.schemas import (
    ActorContext,
    AuditEvent,
    OperationId,
    RecordLocator,
    ResourceType,
)


def _valid_audit_rows(count: int = 4) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous_hash = None
    for sequence in range(1, count + 1):
        event = AuditEvent(
            event_id=f"event-{sequence}",
            sequence=sequence,
            record=RecordLocator(
                resource_type=ResourceType.PATIENT,
                resource_id="rq2-test-record",
            ),
            actor=ActorContext(
                actor_id="administrator-rq2",
                organization_id="org-rq2",
            ),
            action=OperationId.CREATE_RECORD,
            timestamp=datetime(
                2026,
                8,
                9,
                12,
                0,
                sequence,
                tzinfo=timezone.utc,
            ),
        )
        event_hash = audit_event_sha256(event, previous_hash)
        rows.append(
            {
                "sequence": sequence,
                "event_id": event.event_id,
                "resource_type": "Patient",
                "resource_id": "rq2-test-record",
                "actor_id": event.actor.actor_id,
                "organization_id": event.actor.organization_id,
                "action": "OP1",
                "decision": None,
                "event_timestamp": event.timestamp,
                "previous_hash": previous_hash,
                "event_hash": event_hash,
            }
        )
        previous_hash = event_hash
    return rows


def _trial(
    scenario_id: str,
    trial: int,
    *,
    detected: bool | None,
    rejected: bool | None = None,
) -> TamperTrial:
    definition = next(
        item for item in SCENARIOS if item.scenario_id == scenario_id
    )
    protected = definition.protected_write
    expected_met = (
        rejected is True
        if protected
        else detected is definition.expected_detection
    )
    return TamperTrial(
        batch_id="tamper-test",
        scenario_id=scenario_id,
        protocol_mapping=definition.protocol_mapping,
        architecture=definition.architecture,
        trial=trial,
        trial_id=f"{scenario_id}-trial{trial:02d}",
        record_type="Patient",
        record_id=_record_id("tamper-test", scenario_id, trial),
        mutation_class=definition.mutation_class,
        verification_method=definition.verification_method,
        expected_detection=definition.expected_detection,
        detection_applicable=definition.detection_applicable,
        baseline_verification_passed=True,
        baseline_false_positive=False,
        mutation_applied=True,
        detected=detected,
        undetected=(
            not detected
            if definition.detection_applicable and detected is not None
            else None
        ),
        expected_outcome_met=expected_met,
        verification_latency_ms=float(trial),
        external_reference_detected=None,
        first_invalid_sequence_expected=None,
        first_invalid_sequence_observed=None,
        protected_write_attempted=protected,
        protected_write_rejected=rejected,
        protected_state_changed=False if protected else None,
        pre_state_present=False if protected else None,
        post_state_present=False if protected else None,
        gateway_http_status=500 if protected else None,
        gateway_stage="endorse" if protected else None,
        transaction_id="tx-test" if protected else None,
        validation_code=None,
        restoration_verified=True,
        outcome="test",
        started_at_utc="2026-08-09T12:00:00+00:00",
        notes="",
    )


def test_scenario_matrix_is_separate_and_complete() -> None:
    assert len(SCENARIOS) == 8
    assert len({item.scenario_id for item in SCENARIOS}) == 8
    assert sum(item.architecture == "traditional" for item in SCENARIOS) == 6
    assert sum(item.architecture == "fabric" for item in SCENARIOS) == 2
    assert {
        item.scenario_id
        for item in SCENARIOS
        if item.expected_detection is False
    } == {
        "T5_PRIV_PAYLOAD_HASH_REWRITE",
        "T6_PRIV_AUDIT_SUFFIX_REWRITE",
    }
    endorsement = next(
        item
        for item in SCENARIOS
        if item.scenario_id == "F2_INSUFFICIENT_ENDORSEMENT"
    )
    assert endorsement.detection_applicable is False
    assert endorsement.protected_write is True


def test_record_ids_are_unique_and_fhir_compatible() -> None:
    identifiers = {
        _record_id("tamper-test", scenario.scenario_id, trial)
        for scenario in SCENARIOS
        for trial in range(1, 11)
    }
    assert len(identifiers) == 80
    assert all(1 <= len(value) <= 64 for value in identifiers)
    assert all(
        all(character.isalnum() or character in ".-" for character in value)
        for value in identifiers
    )


def test_protected_write_accepts_both_valid_fabric_rejection_paths() -> None:
    assert _protected_write_outcome_met(
        rejected=True,
        state_changed=False,
        gateway_stage="endorse",
        validation_code=None,
    )
    assert _protected_write_outcome_met(
        rejected=True,
        state_changed=False,
        gateway_stage="commit_validation",
        validation_code=10,
    )


def test_protected_write_rejects_wrong_code_or_changed_state() -> None:
    assert not _protected_write_outcome_met(
        rejected=True,
        state_changed=False,
        gateway_stage="commit_validation",
        validation_code=11,
    )
    assert not _protected_write_outcome_met(
        rejected=True,
        state_changed=True,
        gateway_stage="commit_validation",
        validation_code=10,
    )


def test_valid_audit_chain_and_first_broken_link() -> None:
    rows = _valid_audit_rows()
    assert _first_invalid_rows(rows) is None

    modified = [dict(row) for row in rows]
    modified[1]["actor_id"] = "tampered-actor"
    assert _first_invalid_rows(modified) == 2

    deleted = [dict(row) for row in rows]
    del deleted[1]
    assert _first_invalid_rows(deleted) == 3

    reordered = [dict(row) for row in rows]
    reordered[1]["sequence"], reordered[2]["sequence"] = (
        reordered[2]["sequence"],
        reordered[1]["sequence"],
    )
    reordered.sort(key=lambda row: int(row["sequence"]))
    assert _first_invalid_rows(reordered) == 2


def test_summaries_do_not_aggregate_scenarios() -> None:
    trials = []
    for definition in SCENARIOS:
        for trial in range(1, 11):
            if definition.protected_write:
                row = _trial(
                    definition.scenario_id,
                    trial,
                    detected=None,
                    rejected=True,
                )
            else:
                row = _trial(
                    definition.scenario_id,
                    trial,
                    detected=definition.expected_detection,
                )
            trials.append(row)

    summaries = _summaries("tamper-test", trials)
    assert len(summaries) == 8
    assert all(summary.trials == 10 for summary in summaries)
    assert all(summary.baseline_false_positives == 0 for summary in summaries)

    payload = next(
        item for item in summaries if item.scenario_id == "T1_PAYLOAD_ONLY"
    )
    assert payload.detected_trials == 10
    assert payload.undetected_trials == 0
    assert payload.detection_rate == 1.0

    rewrite = next(
        item
        for item in summaries
        if item.scenario_id == "T5_PRIV_PAYLOAD_HASH_REWRITE"
    )
    assert rewrite.detected_trials == 0
    assert rewrite.undetected_trials == 10
    assert rewrite.detection_rate == 0.0

    endorsement = next(
        item
        for item in summaries
        if item.scenario_id == "F2_INSUFFICIENT_ENDORSEMENT"
    )
    assert endorsement.detected_trials is None
    assert endorsement.detection_rate is None
    assert endorsement.protected_write_attempts == 10
    assert endorsement.protected_write_rejections == 10
    assert endorsement.protected_write_rejection_rate == 1.0


def test_summary_preserves_unexpected_scientific_result() -> None:
    original = _trial("T1_PAYLOAD_ONLY", 1, detected=True)
    unexpected = replace(
        original,
        trial=2,
        trial_id="T1_PAYLOAD_ONLY-trial02",
        record_id=_record_id("tamper-test", "T1_PAYLOAD_ONLY", 2),
        detected=False,
        undetected=True,
        expected_outcome_met=False,
        verification_latency_ms=2.0,
    )
    summary = _summaries("tamper-test", [original, unexpected])[0]
    assert summary.trials == 2
    assert summary.detected_trials == 1
    assert summary.undetected_trials == 1
    assert summary.detection_rate == 0.5
    assert summary.expected_outcome_met_trials == 1

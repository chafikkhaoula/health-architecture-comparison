from __future__ import annotations

import pytest

from benchmark.workload import (
    build_operation_plan,
    build_workload_resources,
)
from shared.schemas import (
    AccessDecision,
    ActorContext,
    OperationId,
    UpdateAuthorizationRequest,
)

ADMINISTRATOR = ActorContext(
    actor_id="administrator-benchmark",
    organization_id="org-benchmark",
)
PRINCIPAL = ActorContext(
    actor_id="clinician-benchmark",
    organization_id="org-benchmark",
)


def test_workload_resources_are_deterministic_and_namespaced() -> None:
    first = build_workload_resources(
        6,
        seed=42,
        namespace="pair01",
    )
    second = build_workload_resources(
        6,
        seed=42,
        namespace="pair01",
    )

    assert first == second
    assert len(first) == 6
    assert all(
        resource.resource_id.startswith("pair01-")
        for resource in first
    )

    report = next(
        resource
        for resource in first
        if resource.resource_type.value == "DiagnosticReport"
    )
    assert report.payload["subject"]["reference"].startswith(
        "Patient/pair01-"
    )
    assert report.payload["result"][0]["reference"].startswith(
        "Observation/pair01-"
    )


@pytest.mark.parametrize("operation", list(OperationId))
def test_plan_builds_every_frozen_operation(
    operation: OperationId,
) -> None:
    resources = build_workload_resources(
        3,
        seed=42,
        namespace="pair01",
    )

    plan = build_operation_plan(
        operation,
        resources,
        request_count=3,
        namespace="pair01",
        administrator=ADMINISTRATOR,
        principal=PRINCIPAL,
    )

    assert len(plan) == 3
    assert [item.sequence for item in plan] == [1, 2, 3]
    assert len({item.request_id for item in plan}) == 3
    assert all(item.operation is operation for item in plan)

    expected_status = (
        201 if operation is OperationId.CREATE_RECORD else 200
    )
    assert all(
        item.expected_http_status == expected_status
        for item in plan
    )

    if operation is OperationId.EVALUATE_ACCESS:
        assert all(
            item.expected_authorization_decision
            is AccessDecision.ALLOW
            for item in plan
        )


def test_authorization_plan_freezes_rule_and_actor_semantics() -> None:
    resources = build_workload_resources(
        2,
        seed=42,
        namespace="pair02",
    )
    plan = build_operation_plan(
        OperationId.UPDATE_AUTHORIZATION,
        resources,
        request_count=2,
        namespace="pair02",
        administrator=ADMINISTRATOR,
        principal=PRINCIPAL,
        access_decision=AccessDecision.DENY,
    )

    first = plan[0].payload
    assert isinstance(first, UpdateAuthorizationRequest)
    assert first.actor == ADMINISTRATOR
    assert first.rule.principal == PRINCIPAL
    assert first.rule.decision is AccessDecision.DENY
    assert first.rule.rule_id == "rule-pair02-000001"


def test_plan_rejects_insufficient_resources() -> None:
    resources = build_workload_resources(
        1,
        seed=42,
        namespace="pair03",
    )

    with pytest.raises(
        ValueError,
        match="at least request_count",
    ):
        build_operation_plan(
            OperationId.CREATE_RECORD,
            resources,
            request_count=2,
            namespace="pair03",
            administrator=ADMINISTRATOR,
            principal=PRINCIPAL,
        )

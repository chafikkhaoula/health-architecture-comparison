import asyncio
import json
from datetime import datetime, timezone

import httpx

from benchmark.runner import RunContext
from benchmark.validation import validate_durable_state
from benchmark.workload import build_workload_resources
from shared.hashing.canonical import payload_sha256
from shared.schemas import AccessDecision, ActorContext


def test_durable_validation_checks_payload_hash_and_audit() -> None:
    administrator = ActorContext(
        actor_id="administrator-final",
        organization_id="org-final",
    )
    principal = ActorContext(
        actor_id="clinician-final",
        organization_id="org-final",
    )
    resources = build_workload_resources(
        1,
        seed=42,
        namespace="validation",
    )
    resource = resources[0]
    record = {
        "resource_type": resource.resource_type.value,
        "resource_id": resource.resource_id,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.read())
        if operation == "OP2":
            return httpx.Response(
                200,
                json={"resource": resource.model_dump(mode="json")},
            )
        if operation == "OP5":
            digest = payload_sha256(resource.payload)
            return httpx.Response(
                200,
                json={
                    "record": record,
                    "authoritative_evidence": {
                        "algorithm": "sha256",
                        "canonicalization": "RFC8785",
                        "payload_hash": digest,
                    },
                    "observed_hash": digest,
                    "matches": True,
                },
            )
        assert operation == "OP6"
        assert body["record"] == record
        now = datetime.now(timezone.utc).isoformat()
        return httpx.Response(
            200,
            json={
                "record": record,
                "events": [
                    {
                        "event_id": "event-1",
                        "sequence": 1,
                        "record": record,
                        "actor": administrator.model_dump(mode="json"),
                        "action": "OP1",
                        "decision": None,
                        "timestamp": now,
                    },
                    {
                        "event_id": "event-2",
                        "sequence": 2,
                        "record": record,
                        "actor": administrator.model_dump(mode="json"),
                        "action": "OP3",
                        "decision": None,
                        "timestamp": now,
                    },
                    {
                        "event_id": "event-3",
                        "sequence": 3,
                        "record": record,
                        "actor": principal.model_dump(mode="json"),
                        "action": "OP4",
                        "decision": "ALLOW",
                        "timestamp": now,
                    },
                ],
            },
        )

    context = RunContext(
        batch_id="final-test",
        pair_id="pair-test",
        repetition=1,
        attempt_id="attempt-test",
        architecture="traditional",
        run_id="validation-test",
        workload_size=1,
        concurrency=1,
    )
    observations = asyncio.run(
        validate_durable_state(
            resources,
            context,
            base_url="http://validation.test",
            namespace="validation",
            administrator=administrator,
            principal=principal,
            access_decision=AccessDecision.ALLOW,
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
    )

    assert len(observations) == 3
    assert all(item.correctness_result for item in observations)

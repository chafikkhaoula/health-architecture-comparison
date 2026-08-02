from types import SimpleNamespace

import pytest
from fastapi import Response

from shared.http_api import _attach_receipt_headers


@pytest.mark.parametrize(
    ("receipt", "expected_status"),
    (
        (
            SimpleNamespace(
                transaction_id="tx-valid",
                commit_status="VALID",
                validation_code=0,
                block_number=7,
            ),
            "VALID",
        ),
        (
            SimpleNamespace(
                transaction_id="tx-numeric-valid",
                validation_code=0,
            ),
            "VALID",
        ),
        (
            SimpleNamespace(
                transaction_id="tx-invalid",
                commit_status="MVCC_READ_CONFLICT",
                validation_code=11,
            ),
            "MVCC_READ_CONFLICT",
        ),
    ),
)
def test_receipt_header_uses_canonical_commit_status(
    receipt: SimpleNamespace,
    expected_status: str,
) -> None:
    response = Response()
    adapter = SimpleNamespace(
        last_transaction_receipt=receipt,
    )

    _attach_receipt_headers(response, adapter)

    assert response.headers["X-Fabric-Tx-Id"] == (
        receipt.transaction_id
    )
    assert response.headers[
        "X-Fabric-Validation-Code"
    ] == expected_status
    if hasattr(receipt, "block_number"):
        assert response.headers["X-Fabric-Block-Number"] == "7"
    else:
        assert "X-Fabric-Block-Number" not in response.headers


def test_receipt_headers_are_absent_without_a_receipt() -> None:
    response = Response()
    adapter = SimpleNamespace(
        last_transaction_receipt=None,
    )

    _attach_receipt_headers(response, adapter)

    assert "X-Fabric-Tx-Id" not in response.headers
    assert "X-Fabric-Validation-Code" not in response.headers
    assert "X-Fabric-Block-Number" not in response.headers

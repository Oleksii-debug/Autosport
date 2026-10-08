import pytest

from autosport.real_execution_ledger import (
    EventType,
    ExecutionLedgerIntegrityError,
    RealExecutionLedger,
)


class _TextAlias(str):
    pass


@pytest.mark.parametrize(
    ("field_name", "error_fragment"),
    (
        ("evidence_id", "canonical evidence identity"),
        ("external_receipt_id", "canonical receipt identity"),
    ),
)
def test_reconciliation_replay_rejects_text_alias(
    field_name: str,
    error_fragment: str,
) -> None:
    payload = {
        "evidence_id": "evidence-1",
        "external_receipt_id": "receipt-1",
    }
    payload[field_name] = _TextAlias(payload[field_name])
    events = [
        {"event_type": EventType.ATTEMPT_RESERVED.value, "payload": {}},
        {"event_type": EventType.ATTEMPT_UNKNOWN.value, "payload": {}},
        {"event_type": EventType.RECONCILED_FOUND.value, "payload": payload},
    ]

    with pytest.raises(ExecutionLedgerIntegrityError, match=error_fragment):
        RealExecutionLedger._state(events)

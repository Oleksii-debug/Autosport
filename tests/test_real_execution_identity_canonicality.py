from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionAttempt,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExternalReceiptIdentity,
    RealExecutionLedger,
    _digest,
)


TS = "2026-10-07T00:00:00+00:00"
RESERVED_AT = "2026-10-07T00:00:01+00:00"
EXPIRES_AT = "2026-10-07T00:10:00+00:00"
SHA = "a" * 64


def _action(**overrides: object) -> ExecutionAction:
    values: dict[str, object] = {
        "action_id": "action-1",
        "bookmaker_id": "betfair",
        "account_id": "account-1",
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "side": "BACK",
        "requested_odds": "2.5",
        "requested_stake": "10",
        "quote_id": "quote-1",
        "quote_observed_at": TS,
        "expires_at": EXPIRES_AT,
    }
    values.update(overrides)
    return ExecutionAction(**values)  # type: ignore[arg-type]


def _plan(**overrides: object) -> ExecutionPlan:
    values: dict[str, object] = {
        "plan_id": "plan-1",
        "bookmaker_profile_version": "profile-v1",
        "decision_id": "decision-1",
        "approval_id": "approval-1",
        "created_at": TS,
        "actions": (_action(),),
    }
    values.update(overrides)
    return ExecutionPlan(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    (
        "action_id",
        "bookmaker_id",
        "account_id",
        "event_id",
        "market_id",
        "selection_id",
        "side",
        "quote_id",
    ),
)
def test_execution_action_rejects_padded_identity_alias(field: str) -> None:
    with pytest.raises(ValueError, match="canonical text"):
        _action(**{field: " padded "})


@pytest.mark.parametrize(
    "field",
    ("plan_id", "bookmaker_profile_version", "decision_id", "approval_id"),
)
def test_execution_plan_rejects_padded_identity_alias(field: str) -> None:
    with pytest.raises(ValueError, match="canonical text"):
        _plan(**{field: " padded "})


def test_execution_identity_text_rejects_nul_and_surrogate() -> None:
    with pytest.raises(ValueError, match="canonical text"):
        _action(account_id="account\x00shadow")
    with pytest.raises(ValueError, match="valid UTF-8"):
        _action(account_id="account-\ud800")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempt_id", " attempt-1 "),
        ("plan_id", " plan-1 "),
        ("action_id", " action-1 "),
        ("effect_fingerprint", "A" * 64),
        ("reserved_at", " 2026-10-07T00:00:01+00:00 "),
    ),
)
def test_execution_attempt_is_canonical_at_construction(
    field: str,
    value: str,
) -> None:
    values = {
        "attempt_id": "attempt-1",
        "plan_id": "plan-1",
        "action_id": "action-1",
        "effect_fingerprint": SHA,
        "reserved_at": RESERVED_AT,
    }
    values[field] = value
    with pytest.raises(ValueError):
        ExecutionAttempt(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("bookmaker_id", " betfair "),
        ("account_id", " account-1 "),
        ("external_receipt_id", " receipt-1 "),
    ),
)
def test_external_receipt_identity_rejects_alias_spelling(
    field: str,
    value: str,
) -> None:
    values = {
        "bookmaker_id": "betfair",
        "account_id": "account-1",
        "external_receipt_id": "receipt-1",
    }
    values[field] = value
    with pytest.raises(ValueError, match="canonical text"):
        ExternalReceiptIdentity(**values)


def test_begin_attempt_rejects_padded_identity_before_durable_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())
    before = path.read_bytes()

    with pytest.raises(ValueError, match="canonical text"):
        ledger.begin_attempt(
            plan_id="plan-1",
            action_id="action-1",
            attempt_id=" attempt-1 ",
            reserved_at=RESERVED_AT,
        )

    assert path.read_bytes() == before


def test_restart_rejects_hash_valid_padded_durable_event_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())

    envelope = json.loads(path.read_text(encoding="utf-8"))
    original_event_id = envelope["event"]["event_id"]
    envelope["event"]["event_id"] = f" {original_event_id} "
    envelope["sha256"] = _digest(envelope["event"])
    path.write_text(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="invalid execution event identity",
    ):
        RealExecutionLedger(path).verify_integrity()

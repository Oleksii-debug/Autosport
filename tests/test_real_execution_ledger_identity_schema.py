from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    EventType,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


T0 = "2026-10-07T00:00:00+00:00"
T1 = "2026-10-07T00:01:00+00:00"


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode must not execute")


def _action(**overrides: object) -> ExecutionAction:
    values: dict[str, object] = {
        "action_id": "action-1",
        "bookmaker_id": "book-a",
        "account_id": "acct-a",
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "side": "BACK",
        "requested_odds": Decimal("2"),
        "requested_stake": Decimal("10"),
        "quote_id": "quote-1",
        "quote_observed_at": T0,
        "expires_at": T1,
    }
    values.update(overrides)
    return ExecutionAction(**values)  # type: ignore[arg-type]


def _plan(**overrides: object) -> ExecutionPlan:
    values: dict[str, object] = {
        "plan_id": "plan-1",
        "bookmaker_profile_version": "profile-1",
        "decision_id": "decision-1",
        "approval_id": "approval-1",
        "created_at": T0,
        "actions": (_action(),),
    }
    values.update(overrides)
    return ExecutionPlan(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field_name",
    (
        "action_id",
        "bookmaker_id",
        "account_id",
        "event_id",
        "market_id",
        "selection_id",
        "quote_id",
    ),
)
def test_execution_action_rejects_padded_identity_alias(field_name: str) -> None:
    with pytest.raises(ValueError, match="exact canonical identity text"):
        _action(**{field_name: " alias"})


@pytest.mark.parametrize(
    "field_name",
    ("plan_id", "bookmaker_profile_version", "decision_id", "approval_id"),
)
def test_execution_plan_rejects_str_subclass_identity_before_dispatch(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="exact canonical identity text"):
        _plan(**{field_name: _TrapStr("identity")})


def test_acknowledgement_and_reconciliation_reject_identity_aliases() -> None:
    with pytest.raises(ValueError, match="attempt_id.*exact canonical"):
        ExternalAcknowledgement(
            attempt_id=" attempt-1",
            external_receipt_id="receipt-1",
            status=AcknowledgementStatus.REJECTED,
            acknowledged_at=T0,
        )

    with pytest.raises(ValueError, match="external_receipt_id.*exact canonical"):
        ExternalEffectReconciliation(
            attempt_id="attempt-1",
            evidence_id="evidence-1",
            external_receipt_id="receipt-1\n",
            observed_at=T0,
            source="provider readback",
        )

    with pytest.raises(ValueError, match="evidence_id.*UTF-8"):
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id="evidence\ud800",
            observed_at=T0,
            external_effect_found=False,
            source="provider readback",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("event_id", " event-1"),
        ("plan_id", "plan-1 "),
        ("action_id", "action\n1"),
        ("attempt_id", "attempt\ud800"),
    ),
)
def test_restart_event_validation_rejects_noncanonical_identity(
    field_name: str,
    value: str,
) -> None:
    event: dict[str, object] = {
        "schema_version": 1,
        "event_id": "event-1",
        "event_type": EventType.ATTEMPT_RESERVED.value,
        "recorded_at": T0,
        "plan_id": "plan-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "payload": {},
    }
    event[field_name] = value

    with pytest.raises(ExecutionLedgerIntegrityError, match=f"invalid {field_name}"):
        RealExecutionLedger._validate_event(event, line=1)


def test_valid_execution_identity_payload_is_unchanged() -> None:
    action = _action()
    plan = _plan(actions=(action,))

    assert action.to_dict()["action_id"] == "action-1"
    assert action.to_dict()["quote_id"] == "quote-1"
    assert plan.to_dict()["plan_id"] == "plan-1"
    assert plan.to_dict()["decision_id"] == "decision-1"


def test_execution_attempt_constructor_revalidates_identity_fields() -> None:
    from autosport.real_execution_ledger import ExecutionAttempt

    with pytest.raises(ValueError, match="attempt_id.*exact canonical"):
        ExecutionAttempt(
            attempt_id=" attempt-1",
            plan_id="plan-1",
            action_id="action-1",
            effect_fingerprint="a" * 64,
            reserved_at=T0,
        )


def test_external_receipt_identity_constructor_revalidates_identity_fields() -> None:
    from autosport.real_execution_ledger import ExternalReceiptIdentity

    with pytest.raises(ValueError, match="account_id.*exact canonical"):
        ExternalReceiptIdentity(
            bookmaker_id="book-a",
            account_id=" acct-a",
            external_receipt_id="receipt-1",
        )


def test_execution_plan_fingerprint_rejects_subclass_before_virtual_serialization() -> None:
    class HostilePlan(ExecutionPlan):
        __slots__ = ()

        def to_dict(self) -> dict[str, object]:
            raise AssertionError("ExecutionPlan subclass serialization must not execute")

    base = _plan()
    plan = HostilePlan(
        plan_id=base.plan_id,
        bookmaker_profile_version=base.bookmaker_profile_version,
        decision_id=base.decision_id,
        approval_id=base.approval_id,
        created_at=base.created_at,
        actions=base.actions,
        schema_version=base.schema_version,
    )

    with pytest.raises(ValueError, match="exact ExecutionPlan"):
        _ = plan.fingerprint


def test_plan_readback_rejects_dict_subclass_before_mapping_dispatch() -> None:
    plan = _plan()

    class HostileDict(dict):
        def keys(self):
            raise AssertionError("dict subclass keys must not execute")

        def __iter__(self):
            raise AssertionError("dict subclass iteration must not execute")

    with pytest.raises(ExecutionLedgerIntegrityError, match="stored plan schema is invalid"):
        RealExecutionLedger._plan_from_dict(HostileDict(plan.to_dict()))


def test_plan_readback_rejects_hostile_action_key_before_hash_dispatch() -> None:
    raw = _plan().to_dict()

    class HostileKey(str):
        armed = False

        def __hash__(self):
            if self.armed:
                raise AssertionError("hostile action key hashed before exact admission")
            return str.__hash__(self)

        def __eq__(self, other):
            if self.armed:
                raise AssertionError("hostile action key compared before exact admission")
            return str.__eq__(self, other)

    action = raw["actions"][0]
    key = HostileKey("action_id")
    value = action.pop("action_id")
    action[key] = value
    key.armed = True

    with pytest.raises(ExecutionLedgerIntegrityError, match="stored action schema is invalid"):
        RealExecutionLedger._plan_from_dict(raw)


def test_supervised_approval_lookup_rejects_hostile_identity_subclasses_before_dispatch(
    tmp_path,
) -> None:
    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")

    with pytest.raises(ValueError, match="plan_id.*exact canonical"):
        ledger.supervised_approval_is_active(
            plan_id=_TrapStr("plan-1"),
            approval_id="approval-1",
            approval_fingerprint="a" * 64,
        )
    with pytest.raises(ValueError, match="approval_id.*exact canonical"):
        ledger.supervised_approval_is_active(
            plan_id="plan-1",
            approval_id=_TrapStr("approval-1"),
            approval_fingerprint="a" * 64,
        )
    with pytest.raises(ValueError, match="approval_fingerprint.*non-empty text"):
        ledger.supervised_approval_is_active(
            plan_id="plan-1",
            approval_id="approval-1",
            approval_fingerprint=_TrapStr("a" * 64),
        )


def test_supervised_approval_revoke_rejects_hostile_plan_identity_before_lookup(
    tmp_path,
) -> None:
    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")

    with pytest.raises(ValueError, match="plan_id.*exact canonical"):
        ledger.revoke_supervised_approval(
            plan_id=_TrapStr("plan-1"),
            approval_id="approval-1",
            approval_fingerprint="a" * 64,
            revoked_at=T1,
            revocation_evidence_sha256="b" * 64,
        )


def test_acknowledgement_readback_rejects_hostile_status_before_enum_dispatch() -> None:
    acknowledgement = ExternalAcknowledgement(
        attempt_id="attempt-1",
        external_receipt_id="receipt-1",
        status=AcknowledgementStatus.REJECTED,
        acknowledged_at=T0,
    )
    raw = acknowledgement.to_dict()

    class HostileStatus(str):
        def __hash__(self):
            raise AssertionError("hostile status hashed before exact admission")

        def __eq__(self, other):
            raise AssertionError("hostile status compared before exact admission")

        def strip(self, *args: object, **kwargs: object):
            raise AssertionError("hostile status stripped before exact admission")

    raw["status"] = HostileStatus(raw["status"])
    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="stored acknowledgement values are invalid",
    ):
        RealExecutionLedger._acknowledgement_from_dict(raw)

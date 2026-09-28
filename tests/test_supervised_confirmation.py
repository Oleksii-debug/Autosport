from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import shutil

import pytest

from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
    SupervisedConfirmationIntegrityError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


DECISION_SHA = "d" * 64
APPROVAL_SHA = "a" * 64
RISK_SHA = "b" * 64


def _prepare(authority: SupervisedConfirmationAuthority, **changes):
    values = {
        "review_id": "review-1",
        "decision_id": "decision-1",
        "bookmaker_id": "betfair",
        "account_id": "account-1",
        "decision_sha256": DECISION_SHA,
        "approval_evidence_sha256": APPROVAL_SHA,
        "risk_evidence_sha256": RISK_SHA,
        "review_payload": {
            "decision": "BACK selection-1",
            "requested_odds": "2.10",
            "requested_stake": "5.00",
            "risk": "within external limits",
        },
        "ttl_seconds": 120,
    }
    values.update(changes)
    return authority.prepare_review(**values)


def test_review_confirm_verify_survives_restart(tmp_path):
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)

    clock.advance(seconds=5)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    assert receipt.review_id == review.review_id
    assert receipt.review_sha256 == review.review_sha256
    assert receipt.decision_id == "decision-1"
    assert receipt.bookmaker_id == "betfair"
    assert receipt.account_id == "account-1"
    assert receipt.consumed_at is None

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    verified = reopened.verify_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )
    assert verified == receipt


def test_resolve_receipt_binding_returns_exact_durable_review_provenance(tmp_path):
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)

    clock.advance(seconds=5)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    binding = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )

    assert binding.receipt == receipt
    assert binding.review == review
    assert binding.review.decision_sha256 == DECISION_SHA
    assert binding.review.approval_evidence_sha256 == APPROVAL_SHA
    assert binding.review.risk_evidence_sha256 == RISK_SHA
    assert binding.review.review_payload_sha256 == review.review_payload_sha256

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    assert (
        reopened.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )
        == binding
    )


def test_resolve_receipt_binding_consumed_history_is_audit_only(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=clock
    )
    review = _prepare(authority)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    consumed = authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-1",
    )

    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="already been consumed",
    ):
        authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    audit = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert audit.receipt == consumed
    assert audit.review == review
    assert audit.receipt.consumed_by == "supervised-plan-issuance:plan-1"


def test_resolve_receipt_binding_enforces_ttl_for_authority_but_not_audit(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=clock
    )
    review = _prepare(authority, ttl_seconds=10)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    clock.advance(seconds=10)

    with pytest.raises(SupervisedConfirmationConflictError, match="expired"):
        authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    audit = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert audit.receipt == receipt
    assert audit.review == review


def test_review_payload_digest_is_product_derived_and_changes_with_displayed_content(tmp_path):
    first = SupervisedConfirmationAuthority(tmp_path / "first.jsonl", clock=FakeClock())
    second = SupervisedConfirmationAuthority(tmp_path / "second.jsonl", clock=FakeClock())

    review_a = _prepare(first)
    review_b = _prepare(
        second,
        review_payload={
            "decision": "BACK selection-1",
            "requested_odds": "2.08",
            "requested_stake": "5.00",
            "risk": "within external limits",
        },
    )

    assert review_a.review_payload_sha256 != review_b.review_payload_sha256
    assert review_a.review_sha256 != review_b.review_sha256


def test_confirmation_requires_exact_displayed_review_digest(tmp_path):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=FakeClock()
    )
    review = _prepare(authority)

    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="displayed review",
    ):
        authority.confirm_review(
            review_id=review.review_id,
            expected_review_sha256="f" * 64,
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 1


def test_expired_review_cannot_be_confirmed(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=clock
    )
    review = _prepare(authority, ttl_seconds=10)
    clock.advance(seconds=11)

    with pytest.raises(SupervisedConfirmationConflictError, match="expired"):
        authority.confirm_review(
            review_id=review.review_id,
            expected_review_sha256=review.review_sha256,
        )

    assert authority.path.read_text(encoding="utf-8").count("\n") == 1


def test_review_cannot_be_confirmed_twice(tmp_path):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=clock
    )
    review = _prepare(authority)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    with pytest.raises(SupervisedConfirmationConflictError, match="already has"):
        authority.confirm_review(
            review_id=review.review_id,
            expected_review_sha256=review.review_sha256,
        )

    verified = authority.verify_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )
    assert verified.receipt_id == receipt.receipt_id


def test_receipt_is_single_use_across_restart(tmp_path):
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    consumed = authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="supervised-plan-issuance:plan-1",
    )
    assert consumed.consumed_by == "supervised-plan-issuance:plan-1"
    assert consumed.consumed_at is not None

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    with pytest.raises(SupervisedConfirmationConflictError, match="already been consumed"):
        reopened.verify_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )
    with pytest.raises(SupervisedConfirmationConflictError, match="already been consumed"):
        reopened.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="another-plan",
        )

    audit_copy = reopened.verify_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert audit_copy.consumed_by == "supervised-plan-issuance:plan-1"


def test_caller_cannot_verify_forged_receipt_id(tmp_path):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=FakeClock()
    )
    review = _prepare(authority)

    with pytest.raises(SupervisedConfirmationConflictError, match="not durable"):
        authority.verify_receipt(
            receipt_id="f" * 64,
            expected_review_sha256=review.review_sha256,
        )


def test_duplicate_review_id_is_rejected_even_when_payload_changes(tmp_path):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=FakeClock()
    )
    _prepare(authority)

    with pytest.raises(SupervisedConfirmationConflictError, match="already exists"):
        _prepare(
            authority,
            review_payload={"decision": "different decision surface"},
        )


def test_record_tamper_fails_before_confirmation_authority_is_reopened(tmp_path):
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=FakeClock())
    _prepare(authority)

    event = json.loads(path.read_text(encoding="utf-8"))
    event["payload"]["decision_id"] = "tampered-decision"
    path.write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SupervisedConfirmationIntegrityError, match="digest mismatch"):
        SupervisedConfirmationAuthority(path, clock=FakeClock())


def test_checkpoint_detects_complete_tail_truncation(tmp_path):
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)
    authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(SupervisedConfirmationIntegrityError, match="truncated"):
        SupervisedConfirmationAuthority(path, clock=clock)


def test_nonempty_journal_without_checkpoint_is_rejected(tmp_path):
    path = tmp_path / "operator-confirmations.jsonl"
    path.write_text('{"untrusted":"legacy"}\n', encoding="utf-8")

    with pytest.raises(SupervisedConfirmationIntegrityError, match="missing its checkpoint"):
        SupervisedConfirmationAuthority(path, clock=FakeClock())


def test_invalid_ttl_and_nonfinite_review_payload_fail_before_append(tmp_path):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "operator-confirmations.jsonl", clock=FakeClock()
    )

    with pytest.raises(ValueError, match="ttl_seconds"):
        _prepare(authority, ttl_seconds=0)
    with pytest.raises(ValueError, match="non-finite"):
        _prepare(authority, review_payload={"price": float("nan")})

    assert authority.path.read_bytes() == b""


def test_active_review_and_receipt_form_one_shot_decision_lifecycle(tmp_path):
    clock = FakeClock()
    path = tmp_path / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    first = _prepare(authority, review_id="review-a")

    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="active unconfirmed review",
    ):
        _prepare(
            authority,
            review_id="review-b",
            risk_evidence_sha256="c" * 64,
            review_payload={"decision": "refreshed operator surface"},
        )

    reopened = SupervisedConfirmationAuthority(path, clock=clock)
    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="active unconfirmed review",
    ):
        _prepare(reopened, review_id="review-b")

    clock.advance(seconds=121)
    second = _prepare(reopened, review_id="review-b")
    with pytest.raises(SupervisedConfirmationConflictError, match="expired"):
        reopened.confirm_review(
            review_id=first.review_id,
            expected_review_sha256=first.review_sha256,
        )
    receipt = reopened.confirm_review(
        review_id=second.review_id,
        expected_review_sha256=second.review_sha256,
    )

    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="already has a durable confirmation receipt",
    ):
        _prepare(reopened, review_id="review-c")

    restarted = SupervisedConfirmationAuthority(path, clock=clock)
    verified = restarted.verify_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=second.review_sha256,
    )
    assert verified.receipt_id == receipt.receipt_id
    with pytest.raises(
        SupervisedConfirmationConflictError,
        match="already has a durable confirmation receipt",
    ):
        _prepare(restarted, review_id="review-c")


def test_nonempty_confirmation_state_fails_closed_after_monotonic_authority_loss(
    tmp_path,
    monkeypatch,
):
    machine_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )
    clock = FakeClock()
    path = tmp_path / "workspace" / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    _prepare(authority)
    assert path.read_bytes()
    assert machine_root.is_dir()

    shutil.rmtree(machine_root)

    with pytest.raises(
        SupervisedConfirmationIntegrityError,
        match="activated monotonic authority history is missing under selected root",
    ):
        SupervisedConfirmationAuthority(path, clock=clock)


def test_copied_nonempty_confirmation_state_cannot_be_adopted_in_fresh_namespace(
    tmp_path,
    monkeypatch,
):
    machine_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )
    clock = FakeClock()
    source = tmp_path / "workspace-a" / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(source, clock=clock)
    _prepare(authority)

    copied = tmp_path / "workspace-b" / source.name
    copied.parent.mkdir(parents=True)
    shutil.copy2(source, copied)
    shutil.copy2(
        source.with_name(f"{source.name}.head.json"),
        copied.with_name(f"{copied.name}.head.json"),
    )

    with pytest.raises(
        SupervisedConfirmationIntegrityError,
        match="independent monotonic authority",
    ):
        SupervisedConfirmationAuthority(copied, clock=clock)


def test_audit_only_receipt_resolution_does_not_advance_clock_authority(
    tmp_path,
    monkeypatch,
):
    machine_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(machine_root.resolve()),
    )
    clock = FakeClock()
    path = tmp_path / "workspace" / "operator-confirmations.jsonl"
    authority = SupervisedConfirmationAuthority(path, clock=clock)
    review = _prepare(authority)

    clock.advance(seconds=5)
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    legitimate_time = clock.value + timedelta(seconds=1)

    def monotonic_snapshot():
        return tuple(
            (item.relative_to(machine_root).as_posix(), item.read_bytes())
            for item in sorted(machine_root.rglob("*"))
            if item.is_file()
        )

    journal_before = path.read_bytes()
    checkpoint_before = authority.checkpoint_path.read_bytes()
    monotonic_before = monotonic_snapshot()
    assert monotonic_before

    clock.advance(seconds=3600)
    audit = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert audit.receipt == receipt
    assert audit.review == review
    assert path.read_bytes() == journal_before
    assert authority.checkpoint_path.read_bytes() == checkpoint_before
    assert monotonic_snapshot() == monotonic_before

    clock.value = legitimate_time
    current = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=True,
    )
    assert current.receipt == receipt
    assert current.review == review

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.provider_settlement_receipt import (
    ProviderSettlementReceipt,
    ProviderSettlementReceiptError,
    SettlementDisposition,
    verify_settlement_revision,
)


UTC = timezone.utc


def _receipt(**changes) -> ProviderSettlementReceipt:
    values = {
        "provider": "betfair",
        "provider_receipt_id": "settlement-2026-09-21-0001",
        "execution_id": "execution-0001",
        "market_id": "1.234567890",
        "selection_id": "12345",
        "disposition": SettlementDisposition.WIN,
        "settled_at": datetime(2026, 9, 21, 17, 30, 0, 123456, tzinfo=UTC),
        "rule_id": "football.match-odds.dead-heat-and-void",
        "rule_version": "2026-09-01",
        "rule_sha256": "a" * 64,
        "provider_evidence_sha256": "b" * 64,
        "revision": 0,
        "supersedes_receipt_sha256": None,
    }
    values.update(changes)
    return ProviderSettlementReceipt(**values)


def test_receipt_digest_is_deterministic_and_round_trips() -> None:
    first = _receipt()
    second = _receipt()

    assert first.receipt_sha256 == second.receipt_sha256
    assert first.record_sha256 == first.receipt_sha256
    assert len(first.receipt_sha256) == 64
    assert ProviderSettlementReceipt.from_dict(first.to_dict()) == first


def test_rule_and_provider_evidence_are_part_of_receipt_identity() -> None:
    receipt = _receipt()

    assert (
        replace(receipt, rule_version="2026-09-02").receipt_sha256
        != receipt.receipt_sha256
    )
    assert replace(receipt, rule_sha256="c" * 64).receipt_sha256 != receipt.receipt_sha256
    assert (
        replace(receipt, provider_evidence_sha256="d" * 64).receipt_sha256
        != receipt.receipt_sha256
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("disposition", SettlementDisposition.LOSS),
        ("rule_sha256", "c" * 64),
        ("provider_evidence_sha256", "d" * 64),
    ],
)
def test_same_object_semantic_mutation_invalidates_original_receipt_seal(
    field,
    value,
) -> None:
    receipt = _receipt()
    original_sha256 = receipt.receipt_sha256

    object.__setattr__(receipt, field, value)

    with pytest.raises(
        ProviderSettlementReceiptError,
        match="changed after canonical construction",
    ):
        _ = receipt.receipt_sha256
    with pytest.raises(
        ProviderSettlementReceiptError,
        match="changed after canonical construction",
    ):
        receipt.payload()
    with pytest.raises(
        ProviderSettlementReceiptError,
        match="changed after canonical construction",
    ):
        receipt.to_dict()

    assert len(original_sha256) == 64


def test_void_and_push_are_distinct_terminal_outcomes() -> None:
    voided = _receipt(disposition=SettlementDisposition.VOID)
    pushed = _receipt(disposition=SettlementDisposition.PUSH)

    assert voided.disposition is SettlementDisposition.VOID
    assert pushed.disposition is SettlementDisposition.PUSH
    assert voided.receipt_sha256 != pushed.receipt_sha256


def test_rejects_naive_or_non_utc_settlement_time() -> None:
    with pytest.raises(ProviderSettlementReceiptError, match="timezone-aware UTC"):
        _receipt(settled_at=datetime(2026, 9, 21, 17, 30))

    with pytest.raises(ProviderSettlementReceiptError, match="timezone-aware UTC"):
        _receipt(
            settled_at=datetime(
                2026,
                9,
                21,
                18,
                30,
                tzinfo=timezone(timedelta(hours=1)),
            )
        )


def test_rejects_noncanonical_disposition_digest_and_identifiers() -> None:
    with pytest.raises(ProviderSettlementReceiptError, match="exact SettlementDisposition"):
        _receipt(disposition="win")
    with pytest.raises(ProviderSettlementReceiptError, match="lowercase SHA-256"):
        _receipt(rule_sha256="A" * 64)
    with pytest.raises(ProviderSettlementReceiptError, match="lowercase SHA-256"):
        _receipt(provider_evidence_sha256="not-a-digest")
    with pytest.raises(ProviderSettlementReceiptError, match="canonical string"):
        _receipt(provider=" betfair")
    with pytest.raises(ProviderSettlementReceiptError, match="control characters"):
        _receipt(rule_id="rule\nforged")


def test_revision_shape_is_fail_closed() -> None:
    initial = _receipt()
    with pytest.raises(ProviderSettlementReceiptError, match="cannot supersede"):
        replace(initial, supersedes_receipt_sha256="c" * 64)
    with pytest.raises(ProviderSettlementReceiptError, match="requires predecessor"):
        replace(initial, revision=1)
    with pytest.raises(ProviderSettlementReceiptError, match="non-negative integer"):
        _receipt(revision=True)
    with pytest.raises(ProviderSettlementReceiptError, match="non-negative integer"):
        _receipt(revision=-1)


def test_valid_revision_hash_links_immediate_predecessor() -> None:
    previous = _receipt(disposition=SettlementDisposition.WIN)
    current = _receipt(
        disposition=SettlementDisposition.VOID,
        settled_at=previous.settled_at + timedelta(minutes=3),
        rule_version="2026-09-21-hotfix",
        rule_sha256="c" * 64,
        provider_evidence_sha256="d" * 64,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )

    verify_settlement_revision(previous, current)
    assert current.receipt_sha256 != previous.receipt_sha256


def test_semantic_correction_requires_changed_authoritative_basis() -> None:
    previous = _receipt()
    unchanged_basis = _receipt(
        disposition=SettlementDisposition.LOSS,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )

    with pytest.raises(
        ProviderSettlementReceiptError,
        match="requires changed provider evidence or settlement rule basis",
    ):
        verify_settlement_revision(previous, unchanged_basis)


def test_rule_labels_without_new_rule_artifact_do_not_authorize_outcome_change() -> None:
    previous = _receipt()
    label_only = _receipt(
        disposition=SettlementDisposition.LOSS,
        rule_id="football.match-odds.renamed",
        rule_version="2026-09-22-label-only",
        rule_sha256=previous.rule_sha256,
        provider_evidence_sha256=previous.provider_evidence_sha256,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )

    with pytest.raises(
        ProviderSettlementReceiptError,
        match="requires changed provider evidence or settlement rule basis",
    ):
        verify_settlement_revision(previous, label_only)


@pytest.mark.parametrize(
    "basis_change",
    [
        {"provider_evidence_sha256": "d" * 64},
        {"rule_sha256": "c" * 64},
    ],
)
def test_semantic_correction_accepts_new_provider_or_rule_artifact_evidence(
    basis_change,
) -> None:
    previous = _receipt()
    current = _receipt(
        disposition=SettlementDisposition.LOSS,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
        **basis_change,
    )

    verify_settlement_revision(previous, current)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"provider": "other"}, "provider"),
        ({"provider_receipt_id": "other"}, "provider_receipt_id"),
        ({"execution_id": "other"}, "execution_id"),
        ({"market_id": "other"}, "market_id"),
        ({"selection_id": "other"}, "selection_id"),
    ],
)
def test_revision_rejects_identity_changes(mutation, message) -> None:
    previous = _receipt()
    current = _receipt(
        **mutation,
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )

    with pytest.raises(ProviderSettlementReceiptError, match=message):
        verify_settlement_revision(previous, current)


def test_revision_rejects_skips_wrong_predecessor_and_backward_time() -> None:
    previous = _receipt()

    skipped = _receipt(
        revision=2,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )
    with pytest.raises(ProviderSettlementReceiptError, match="exactly one"):
        verify_settlement_revision(previous, skipped)

    wrong_predecessor = _receipt(
        revision=1,
        supersedes_receipt_sha256="f" * 64,
    )
    with pytest.raises(ProviderSettlementReceiptError, match="predecessor digest"):
        verify_settlement_revision(previous, wrong_predecessor)

    backwards = _receipt(
        settled_at=previous.settled_at - timedelta(microseconds=1),
        revision=1,
        supersedes_receipt_sha256=previous.receipt_sha256,
    )
    with pytest.raises(ProviderSettlementReceiptError, match="backwards"):
        verify_settlement_revision(previous, backwards)


def test_from_dict_rejects_digest_tamper_unknown_keys_and_noncanonical_timestamp() -> None:
    receipt = _receipt()

    tampered = receipt.to_dict()
    tampered["rule_version"] = "forged"
    with pytest.raises(ProviderSettlementReceiptError, match="digest mismatch"):
        ProviderSettlementReceipt.from_dict(tampered)

    extra = receipt.to_dict()
    extra["unexpected"] = True
    with pytest.raises(ProviderSettlementReceiptError, match="keys mismatch"):
        ProviderSettlementReceipt.from_dict(extra)

    noncanonical_time = receipt.to_dict()
    noncanonical_time["settled_at"] = "2026-09-21T17:30:00Z"
    with pytest.raises(ProviderSettlementReceiptError, match="canonical microsecond"):
        ProviderSettlementReceipt.from_dict(noncanonical_time)


def test_from_dict_rejects_json_bool_revision_alias() -> None:
    raw = _receipt().to_dict()
    raw["revision"] = False

    with pytest.raises(ProviderSettlementReceiptError, match="revision must be an integer"):
        ProviderSettlementReceipt.from_dict(raw)


def test_from_dict_rejects_json_bool_schema_version_alias() -> None:
    raw = _receipt().to_dict()
    raw["schema_version"] = True

    with pytest.raises(ProviderSettlementReceiptError, match="unsupported settlement"):
        ProviderSettlementReceipt.from_dict(raw)

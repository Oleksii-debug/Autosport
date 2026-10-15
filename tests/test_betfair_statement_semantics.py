from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext
from hashlib import sha256
import json

import pytest

from autosport.betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
)
from autosport.betfair_statement_semantics import (
    BetfairStatementSemanticError,
    assert_betfair_statement_semantic_authoritative,
    classify_betfair_statement_item,
    verify_betfair_statement_semantic_evidence,
)


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _raw(
    transaction_type: object = "ACCOUNT_CREDIT",
    win_lose: object = "RESULT_WON",
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "transactionType": transaction_type,
        "winLose": win_lose,
        "eventId": 28127348,
    }
    if extra:
        payload.update(extra)
    return {
        "unknownStatementItem": json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def _row(
    item_class_data: dict[str, object],
    *,
    ref_id: str = "10841763486",
    amount: Decimal = Decimal("18"),
    balance: Decimal = Decimal("1000035.1"),
    item_class: str = "UNKNOWN",
) -> BetfairAccountStatementItemObservation:
    return BetfairAccountStatementItemObservation(
        ref_id=ref_id,
        item_date="2018-06-07T09:30:27.000Z",
        amount=amount,
        balance=balance,
        item_class=item_class,
        item_class_data_sha256=_canonical_sha256(item_class_data),
    )


def test_result_err_is_label_not_new_balance_effect() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_ERR")

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.transaction_type == "ACCOUNT_CREDIT"
    assert evidence.win_lose == "RESULT_ERR"
    assert evidence.classification_state == "RESTATED_ERROR_LABEL"
    assert evidence.economic_effect == "NO_NEW_BALANCE_EFFECT"
    assert evidence.commission_reversal is False


def test_result_fix_is_balance_correction() -> None:
    raw = _raw("ACCOUNT_DEBIT", "RESULT_FIX")

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.classification_state == "BALANCE_CORRECTION"
    assert evidence.economic_effect == "BALANCE_CORRECTION"
    assert evidence.commission_reversal is False


def test_commission_reversal_fix_is_explicit_correction() -> None:
    raw = _raw("COMMISSION_REVERSAL", "RESULT_FIX")

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.classification_state == "COMMISSION_REVERSAL_CORRECTION"
    assert evidence.economic_effect == "BALANCE_CORRECTION"
    assert evidence.commission_reversal is True


@pytest.mark.parametrize(
    ("win_lose", "expected_state"),
    [
        ("RESULT_WON", "SETTLEMENT_RESULT_WON"),
        ("RESULT_LOST", "SETTLEMENT_RESULT_LOST"),
        ("RESULT_NOT_APPLICABLE", "NON_RESULT_ACCOUNT_MOVEMENT"),
    ],
)
def test_documented_non_fix_result_semantics(
    win_lose: str,
    expected_state: str,
) -> None:
    raw = _raw("ACCOUNT_CREDIT", win_lose)

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.classification_state == expected_state
    assert evidence.economic_effect == "ACCOUNT_MOVEMENT"
    assert evidence.commission_reversal is False


def test_documented_commission_reversal_winlose_is_correction() -> None:
    raw = _raw("COMMISSION_REVERSAL", "COMMISSION_REVERSAL")

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.classification_state == "COMMISSION_REVERSAL_CORRECTION"
    assert evidence.economic_effect == "BALANCE_CORRECTION"
    assert evidence.commission_reversal is True


@pytest.mark.parametrize(
    ("transaction_type", "win_lose"),
    [
        ("NEW_PROVIDER_TYPE", "RESULT_WON"),
        (None, "RESULT_WON"),
        (True, "RESULT_WON"),
        ("ACCOUNT_CREDIT", "NEW_PROVIDER_RESULT"),
        ("ACCOUNT_CREDIT", None),
        ("ACCOUNT_CREDIT", True),
    ],
)
def test_unknown_or_noncanonical_semantics_fail_closed_without_zeroing_money(
    transaction_type: object,
    win_lose: object,
) -> None:
    raw = _raw(transaction_type, win_lose)

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.classification_state == "UNCLASSIFIED_ACCOUNT_MOVEMENT"
    assert evidence.economic_effect == "UNKNOWN"
    assert evidence.commission_reversal is False


def test_raw_item_class_data_must_match_captured_digest() -> None:
    captured = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    substituted = _raw("ACCOUNT_DEBIT", "RESULT_FIX")

    with pytest.raises(
        BetfairStatementSemanticError,
        match="does not match captured row digest",
    ):
        classify_betfair_statement_item(_row(captured), substituted)


@pytest.mark.parametrize(
    "unknown_statement_item",
    [
        "transactionType=ACCOUNT_CREDIT,winLose=RESULT_WON",
        '{"transactionType":"ACCOUNT_CREDIT"',
        (
            '{"transactionType":"ACCOUNT_CREDIT",'
            '"transactionType":"ACCOUNT_DEBIT",'
            '"winLose":"RESULT_FIX"}'
        ),
        (
            '{"transactionType":"ACCOUNT_CREDIT",'
            '"winLose":"RESULT_WON","value":NaN}'
        ),
    ],
    ids=[
        "documented-opaque-string-form",
        "malformed-json",
        "duplicate-semantic-key",
        "non-standard-numeric-constant",
    ],
)
def test_opaque_or_ambiguous_unknown_statement_item_remains_unclassified(
    unknown_statement_item: str,
) -> None:
    raw = {"unknownStatementItem": unknown_statement_item}

    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert evidence.transaction_type is None
    assert evidence.win_lose is None
    assert evidence.classification_state == "UNCLASSIFIED_ACCOUNT_MOVEMENT"
    assert evidence.economic_effect == "UNKNOWN"
    assert evidence.commission_reversal is False


def test_mapping_subclass_cannot_drive_semantic_classification() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")

    class _HostileMapping(dict[str, object]):
        pass

    with pytest.raises(
        BetfairStatementSemanticError,
        match="exact JSON object",
    ):
        classify_betfair_statement_item(_row(raw), _HostileMapping(raw))


def test_unknown_item_class_rejects_unbound_outer_semantic_fields() -> None:
    raw = {
        **_raw("ACCOUNT_CREDIT", "RESULT_WON"),
        "callerSemanticOverride": "RESULT_FIX",
    }

    with pytest.raises(
        BetfairStatementSemanticError,
        match="must contain only unknownStatementItem",
    ):
        classify_betfair_statement_item(_row(raw), raw)


def test_other_item_class_remains_unclassified() -> None:
    raw = {"opaque": "provider-specific"}

    evidence = classify_betfair_statement_item(
        _row(raw, item_class="ACCOUNT"),
        raw,
    )

    assert evidence.transaction_type is None
    assert evidence.win_lose is None
    assert evidence.classification_state == "UNCLASSIFIED_ACCOUNT_MOVEMENT"
    assert evidence.economic_effect == "UNKNOWN"


def test_same_ref_and_item_date_siblings_keep_distinct_semantic_identity() -> None:
    first_raw = _raw("ACCOUNT_CREDIT", "RESULT_ERR")
    second_raw = _raw("ACCOUNT_DEBIT", "RESULT_FIX")
    first = _row(first_raw, amount=Decimal("18"))
    second = _row(second_raw, amount=Decimal("-18"))

    first_evidence = classify_betfair_statement_item(first, first_raw)
    second_evidence = classify_betfair_statement_item(second, second_raw)

    assert first.ref_id == second.ref_id
    assert first.item_date == second.item_date
    assert first_evidence.evidence_sha256 != second_evidence.evidence_sha256




def test_semantic_identity_is_decimal_context_independent() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    captured = _row(
        raw,
        amount=Decimal("123456789.12345678901234567890"),
        balance=Decimal("999999999.999999999999"),
    )

    with localcontext() as context:
        context.prec = 2
        low_precision = classify_betfair_statement_item(captured, raw)
    with localcontext() as context:
        context.prec = 50
        high_precision = classify_betfair_statement_item(captured, raw)

    assert low_precision.evidence_sha256 == high_precision.evidence_sha256


def test_caller_mutation_after_classification_cannot_relabel_evidence() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_ERR")
    captured = _row(raw)

    evidence = classify_betfair_statement_item(captured, raw)
    raw["unknownStatementItem"] = _raw(
        "ACCOUNT_CREDIT", "RESULT_WON"
    )["unknownStatementItem"]

    assert evidence.classification_state == "RESTATED_ERROR_LABEL"
    assert evidence.economic_effect == "NO_NEW_BALANCE_EFFECT"
    assert evidence.row_item_class_data_sha256 == captured.item_class_data_sha256


def test_semantic_evidence_cannot_be_relabelled_without_digest_change() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_ERR")
    evidence = classify_betfair_statement_item(_row(raw), raw)

    with pytest.raises(
        BetfairStatementSemanticError,
        match="canonical classification",
    ):
        replace(evidence, economic_effect="UNKNOWN")



def _semantic_digest_for_test(
    evidence,
    *,
    win_lose: str,
    classification_state: str,
    economic_effect: str,
    commission_reversal: bool = False,
) -> str:
    return _canonical_sha256(
        {
            "schema": "autosport.betfair_statement_row_semantics",
            "schema_version": 1,
            "row": {
                "ref_id": evidence.row_ref_id,
                "item_date": evidence.row_item_date,
                "amount": str(evidence.row_amount),
                "balance": str(evidence.row_balance),
                "item_class": evidence.row_item_class,
                "item_class_data_sha256": evidence.row_item_class_data_sha256,
            },
            "transaction_type": evidence.transaction_type,
            "win_lose": win_lose,
            "classification_state": classification_state,
            "economic_effect": economic_effect,
            "commission_reversal": commission_reversal,
        }
    )


def test_classifier_issued_object_has_process_local_semantic_authority() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    evidence = classify_betfair_statement_item(_row(raw), raw)

    assert_betfair_statement_semantic_authoritative(evidence)


def test_copy_equal_semantic_dto_does_not_inherit_process_local_authority() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    evidence = classify_betfair_statement_item(_row(raw), raw)
    copied = replace(evidence)

    assert copied == evidence
    with pytest.raises(
        BetfairStatementSemanticError,
        match="lacks canonical classifier issuance",
    ):
        assert_betfair_statement_semantic_authoritative(copied)


def test_copy_equal_semantics_can_be_freshly_rederived_from_exact_raw_row() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    row = _row(raw)
    evidence = classify_betfair_statement_item(row, raw)
    copied = replace(evidence)

    assert verify_betfair_statement_semantic_evidence(
        row=row,
        item_class_data=raw,
        evidence=copied,
    )


def test_consistent_rehashed_relabel_is_not_authoritative_and_fails_reresolution() -> None:
    raw = _raw("ACCOUNT_CREDIT", "RESULT_WON")
    row = _row(raw)
    evidence = classify_betfair_statement_item(row, raw)
    forged = replace(
        evidence,
        win_lose="RESULT_ERR",
        classification_state="RESTATED_ERROR_LABEL",
        economic_effect="NO_NEW_BALANCE_EFFECT",
        evidence_sha256=_semantic_digest_for_test(
            evidence,
            win_lose="RESULT_ERR",
            classification_state="RESTATED_ERROR_LABEL",
            economic_effect="NO_NEW_BALANCE_EFFECT",
        ),
    )

    # The DTO is internally coherent, but it is not a product-issued
    # interpretation of the bound raw itemClassData.
    assert forged.evidence_sha256 != evidence.evidence_sha256
    with pytest.raises(
        BetfairStatementSemanticError,
        match="lacks canonical classifier issuance",
    ):
        assert_betfair_statement_semantic_authoritative(forged)
    assert not verify_betfair_statement_semantic_evidence(
        row=row,
        item_class_data=raw,
        evidence=forged,
    )

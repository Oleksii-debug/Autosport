"""Dependent falsifier for PR #966 statement-semantic self-hash authority."""

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from autosport.betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
)
from autosport.betfair_statement_semantics import (
    BetfairStatementSemanticError,
    classify_betfair_statement_item,
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


def _won_raw() -> dict[str, object]:
    nested = {
        "transactionType": "ACCOUNT_CREDIT",
        "winLose": "RESULT_WON",
        "eventId": 28127348,
    }
    return {
        "unknownStatementItem": json.dumps(
            nested,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    }


def _row(raw: dict[str, object]) -> BetfairAccountStatementItemObservation:
    return BetfairAccountStatementItemObservation(
        ref_id="10841763486",
        item_date="2018-06-07T09:30:27.000Z",
        amount=Decimal("18"),
        balance=Decimal("1000035.1"),
        item_class="UNKNOWN",
        item_class_data_sha256=_canonical_sha256(raw),
    )


def _forged_restatement_digest(evidence) -> str:
    projection = {
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
        "win_lose": evidence.win_lose,
        "classification_state": "RESTATED_ERROR_LABEL",
        "economic_effect": "NO_NEW_BALANCE_EFFECT",
        "commission_reversal": False,
    }
    return _canonical_sha256(projection)


def test_valid_self_hash_cannot_relabel_won_row_as_no_new_balance_effect() -> None:
    raw = _won_raw()
    evidence = classify_betfair_statement_item(_row(raw), raw)
    assert evidence.win_lose == "RESULT_WON"
    assert evidence.classification_state == "SETTLEMENT_RESULT_WON"
    assert evidence.economic_effect == "ACCOUNT_MOVEMENT"

    forged_digest = _forged_restatement_digest(evidence)

    # Recomputing an ordinary content hash over caller-selected semantic fields
    # must not be sufficient to change the economic interpretation of an already
    # bound provider row. Canonical semantics need re-derivation/verification from
    # the exact raw itemClassData (or equivalent product-owned issuance authority).
    with pytest.raises(BetfairStatementSemanticError):
        replace(
            evidence,
            classification_state="RESTATED_ERROR_LABEL",
            economic_effect="NO_NEW_BALANCE_EFFECT",
            evidence_sha256=forged_digest,
        )

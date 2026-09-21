"""Fail-closed semantic classification for Betfair account-statement rows.

The canonical provider billing reader intentionally persists only a SHA-256 digest
of ``itemClassData``.  This module accepts the exact captured row plus the raw
``itemClassData`` object, verifies that object against the captured digest, and
then interprets only the documented Betfair ``winLose`` / ``transactionType``
semantics needed for settlement-restatement accounting.

The result is descriptive evidence, not provenance by itself.  Positive consumers
must still bind the row to a product-issued provider observation (for example via
the existing provider-billing authority boundary).  This module does not infer a
``betId`` join, per-bet commission allocation, execution acceptance, or realized
real-money profitability.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Mapping

from .betfair_account_readonly import BetfairReadOnlyError
from .betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
)


class BetfairStatementSemanticError(BetfairReadOnlyError):
    """Raised when statement semantic evidence cannot be classified safely."""


_CLASSIFICATION_STATES = frozenset(
    {
        "RESTATED_ERROR_LABEL",
        "BALANCE_CORRECTION",
        "COMMISSION_REVERSAL_CORRECTION",
        "SETTLEMENT_RESULT_WON",
        "SETTLEMENT_RESULT_LOST",
        "NON_RESULT_ACCOUNT_MOVEMENT",
        "UNCLASSIFIED_ACCOUNT_MOVEMENT",
    }
)
_ECONOMIC_EFFECTS = frozenset(
    {"NO_NEW_BALANCE_EFFECT", "BALANCE_CORRECTION", "ACCOUNT_MOVEMENT", "UNKNOWN"}
)
_KNOWN_TRANSACTION_TYPES = frozenset(
    {"ACCOUNT_DEBIT", "ACCOUNT_CREDIT", "COMMISSION_REVERSAL"}
)
_KNOWN_WIN_LOSE = frozenset(
    {
        "RESULT_ERR",
        "RESULT_FIX",
        "RESULT_WON",
        "RESULT_LOST",
        "RESULT_NOT_APPLICABLE",
        "COMMISSION_REVERSAL",
    }
)


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStatementSemanticError(
            "statement itemClassData is not canonical JSON"
        ) from exc
    return sha256(payload).hexdigest()


def _required_canonical_text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetfairStatementSemanticError(
            f"{field} must be a non-empty canonical string"
        )
    return value


def _optional_semantic_text(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        return None
    return value


def _decode_unknown_statement_item(raw: str) -> Mapping[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise BetfairStatementSemanticError(
                    "unknownStatementItem contains duplicate object key"
                )
            result[key] = value
        return result

    def constant(_value: str) -> object:
        raise BetfairStatementSemanticError(
            "unknownStatementItem contains non-standard numeric constant"
        )

    try:
        decoded = json.loads(
            raw,
            parse_float=Decimal,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except BetfairStatementSemanticError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BetfairStatementSemanticError(
            "unknownStatementItem is not valid JSON"
        ) from exc
    if type(decoded) is not dict or any(
        type(key) is not str for key in decoded
    ):
        raise BetfairStatementSemanticError(
            "unknownStatementItem must decode to a JSON object"
        )
    return decoded


def _semantic_projection(
    *,
    row_ref_id: str,
    row_item_date: str,
    row_amount: Decimal,
    row_balance: Decimal,
    row_item_class: str,
    row_item_class_data_sha256: str,
    transaction_type: str | None,
    win_lose: str | None,
    classification_state: str,
    economic_effect: str,
    commission_reversal: bool,
) -> dict[str, object]:
    return {
        "schema": "autosport.betfair_statement_row_semantics",
        "schema_version": 1,
        "row": {
            "ref_id": row_ref_id,
            "item_date": row_item_date,
            "amount": str(row_amount),
            "balance": str(row_balance),
            "item_class": row_item_class,
            "item_class_data_sha256": row_item_class_data_sha256,
        },
        "transaction_type": transaction_type,
        "win_lose": win_lose,
        "classification_state": classification_state,
        "economic_effect": economic_effect,
        "commission_reversal": commission_reversal,
    }


@dataclass(frozen=True, slots=True)
class BetfairStatementRowSemanticEvidence:
    """Deterministic descriptive interpretation of one captured statement row."""

    row_ref_id: str
    row_item_date: str
    row_amount: Decimal
    row_balance: Decimal
    row_item_class: str
    row_item_class_data_sha256: str
    transaction_type: str | None
    win_lose: str | None
    classification_state: str
    economic_effect: str
    commission_reversal: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        _required_canonical_text(self.row_ref_id, "row_ref_id")
        _required_canonical_text(self.row_item_date, "row_item_date")
        if type(self.row_amount) is not Decimal or not self.row_amount.is_finite():
            raise BetfairStatementSemanticError("row_amount must be finite Decimal")
        if type(self.row_balance) is not Decimal or not self.row_balance.is_finite():
            raise BetfairStatementSemanticError("row_balance must be finite Decimal")
        _required_canonical_text(self.row_item_class, "row_item_class")
        digest = _required_canonical_text(
            self.row_item_class_data_sha256,
            "row_item_class_data_sha256",
        )
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise BetfairStatementSemanticError(
                "row_item_class_data_sha256 must be lowercase SHA-256"
            )
        if self.transaction_type is not None:
            _required_canonical_text(self.transaction_type, "transaction_type")
        if self.win_lose is not None:
            _required_canonical_text(self.win_lose, "win_lose")
        if self.classification_state not in _CLASSIFICATION_STATES:
            raise BetfairStatementSemanticError("unsupported classification_state")
        if self.economic_effect not in _ECONOMIC_EFFECTS:
            raise BetfairStatementSemanticError("unsupported economic_effect")
        if type(self.commission_reversal) is not bool:
            raise BetfairStatementSemanticError("commission_reversal must be bool")
        evidence = _required_canonical_text(self.evidence_sha256, "evidence_sha256")
        if len(evidence) != 64 or any(
            ch not in "0123456789abcdef" for ch in evidence
        ):
            raise BetfairStatementSemanticError(
                "evidence_sha256 must be lowercase SHA-256"
            )
        expected = _canonical_sha256(
            _semantic_projection(
                row_ref_id=self.row_ref_id,
                row_item_date=self.row_item_date,
                row_amount=self.row_amount,
                row_balance=self.row_balance,
                row_item_class=self.row_item_class,
                row_item_class_data_sha256=self.row_item_class_data_sha256,
                transaction_type=self.transaction_type,
                win_lose=self.win_lose,
                classification_state=self.classification_state,
                economic_effect=self.economic_effect,
                commission_reversal=self.commission_reversal,
            )
        )
        if self.evidence_sha256 != expected:
            raise BetfairStatementSemanticError(
                "statement semantic evidence digest mismatch"
            )


def classify_betfair_statement_item(
    row: BetfairAccountStatementItemObservation,
    item_class_data: dict[str, object],
) -> BetfairStatementRowSemanticEvidence:
    """Classify one captured statement row without widening monetary authority."""

    if type(row) is not BetfairAccountStatementItemObservation:
        raise TypeError(
            "row must be exact BetfairAccountStatementItemObservation"
        )
    if type(item_class_data) is not dict or any(
        type(key) is not str for key in item_class_data
    ):
        raise BetfairStatementSemanticError(
            "item_class_data must be an exact JSON object"
        )
    if _canonical_sha256(item_class_data) != row.item_class_data_sha256:
        raise BetfairStatementSemanticError(
            "item_class_data does not match captured row digest"
        )

    transaction_type: str | None = None
    win_lose: str | None = None
    classification_state = "UNCLASSIFIED_ACCOUNT_MOVEMENT"
    economic_effect = "UNKNOWN"
    commission_reversal = False

    if row.item_class == "UNKNOWN":
        if set(item_class_data) != {"unknownStatementItem"}:
            raise BetfairStatementSemanticError(
                "UNKNOWN itemClassData must contain only unknownStatementItem"
            )
        nested_raw = item_class_data["unknownStatementItem"]
        if type(nested_raw) is not str or not nested_raw:
            raise BetfairStatementSemanticError(
                "unknownStatementItem must be non-empty text"
            )
        try:
            nested = _decode_unknown_statement_item(nested_raw)
        except BetfairStatementSemanticError:
            nested = None
        if nested is not None:
            transaction_type = _optional_semantic_text(
                nested.get("transactionType")
            )
            win_lose = _optional_semantic_text(nested.get("winLose"))

        if (
            transaction_type in _KNOWN_TRANSACTION_TYPES
            and win_lose in _KNOWN_WIN_LOSE
        ):
            if win_lose == "RESULT_ERR":
                classification_state = "RESTATED_ERROR_LABEL"
                economic_effect = "NO_NEW_BALANCE_EFFECT"
            elif win_lose == "RESULT_FIX":
                if transaction_type == "COMMISSION_REVERSAL":
                    classification_state = "COMMISSION_REVERSAL_CORRECTION"
                    commission_reversal = True
                else:
                    classification_state = "BALANCE_CORRECTION"
                economic_effect = "BALANCE_CORRECTION"
            elif win_lose == "COMMISSION_REVERSAL":
                classification_state = "COMMISSION_REVERSAL_CORRECTION"
                economic_effect = "BALANCE_CORRECTION"
                commission_reversal = True
            elif win_lose == "RESULT_WON":
                classification_state = "SETTLEMENT_RESULT_WON"
                economic_effect = "ACCOUNT_MOVEMENT"
            elif win_lose == "RESULT_LOST":
                classification_state = "SETTLEMENT_RESULT_LOST"
                economic_effect = "ACCOUNT_MOVEMENT"
            elif win_lose == "RESULT_NOT_APPLICABLE":
                classification_state = "NON_RESULT_ACCOUNT_MOVEMENT"
                economic_effect = "ACCOUNT_MOVEMENT"

    projection = _semantic_projection(
        row_ref_id=row.ref_id,
        row_item_date=row.item_date,
        row_amount=row.amount,
        row_balance=row.balance,
        row_item_class=row.item_class,
        row_item_class_data_sha256=row.item_class_data_sha256,
        transaction_type=transaction_type,
        win_lose=win_lose,
        classification_state=classification_state,
        economic_effect=economic_effect,
        commission_reversal=commission_reversal,
    )
    return BetfairStatementRowSemanticEvidence(
        row_ref_id=row.ref_id,
        row_item_date=row.item_date,
        row_amount=row.amount,
        row_balance=row.balance,
        row_item_class=row.item_class,
        row_item_class_data_sha256=row.item_class_data_sha256,
        transaction_type=transaction_type,
        win_lose=win_lose,
        classification_state=classification_state,
        economic_effect=economic_effect,
        commission_reversal=commission_reversal,
        evidence_sha256=_canonical_sha256(projection),
    )

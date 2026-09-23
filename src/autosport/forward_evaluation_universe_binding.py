from __future__ import annotations

"""Bind forward-economic source receipts to the canonical durable evaluation universe.

This module is a composition layer only.  It does not create a second source-universe
store and it never treats caller-supplied ``AuthoritativeSourceReceipt`` values as
positive authority.  Positive membership is reloaded from
``ProviderEvaluationUniverseStore`` and compared against the forward opportunity
sequence before receipts are emitted for ``forward_evidence_completeness``.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from .evaluation_universe import AttritionReason, EvaluationRow, SlotState
from .forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    UniverseResult,
)
from .provider_evaluation_universe import ProviderEvaluationUniverseStore


_CANONICAL_PROVIDER_UNIVERSE_LOAD = ProviderEvaluationUniverseStore.load


_DOMAIN = "autosport.forward-evaluation-universe-binding.v1"
FORWARD_UNIVERSE_RULE_ID = "autosport.provider-evaluation-universe-forward-rule.v1"


class ForwardEvaluationUniverseBindingError(ValueError):
    """The forward campaign does not match product-owned source-universe authority."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ForwardEvaluationUniverseBindingError(
            "forward source-universe binding must be finite canonical JSON"
        ) from exc


def _digest(payload: Mapping[str, Any]) -> str:
    envelope = {"domain": _DOMAIN, "payload": dict(payload)}
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _instant(value: str, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ForwardEvaluationUniverseBindingError(
            f"{name} must be non-empty canonical ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardEvaluationUniverseBindingError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardEvaluationUniverseBindingError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(UTC)


_RULE_PAYLOAD = {
    "rule_id": FORWARD_UNIVERSE_RULE_ID,
    "authority": "ProviderEvaluationUniverseStore",
    "membership": "exact durable EvaluationUniverse rows",
    "result_by_slot_state": {
        SlotState.CANDIDATE.value: UniverseResult.ADMITTED.value,
        SlotState.NO_EVENT.value: UniverseResult.EXCLUDED.value,
        SlotState.NO_QUOTE.value: UniverseResult.EXCLUDED.value,
        SlotState.SOURCE_OUTAGE.value: UniverseResult.EXCLUDED.value,
        SlotState.NO_CANDIDATE.value: UniverseResult.EXCLUDED.value,
        SlotState.WAIT_ZERO.value: UniverseResult.EXCLUDED.value,
    },
    "reason": "CANDIDATE for admitted rows; canonical attrition_reason for excluded rows",
    "provider_acquisition_state": "exact SlotState value",
    "opportunity_identity": "evaluation-universe-opportunity:<EvaluationRow.row_id>",
    "receipt_identity": "evaluation-universe-receipt:<EvaluationRow.row_id>",
    "observation_interval": "EvaluationRow.source_at..EvaluationRow.committed_at",
    "causal_cutoff": "EvaluationRow.committed_at",
    "receipt_payload": "forward protocol + immutable universe + full canonical EvaluationRow",
}
FORWARD_UNIVERSE_RULE_SHA256 = _digest(_RULE_PAYLOAD)


@dataclass(frozen=True, slots=True)
class ForwardUniverseMemberExpectation:
    """Deterministic consumer projection; authority is established only by re-resolution."""

    row_id: str
    opportunity_id: str
    source_receipt_id: str
    source_receipt_sha256: str
    universe_rule_result: UniverseResult
    universe_rule_reason_code: str
    provider_acquisition_state: str
    observed_lower: datetime
    observed_upper: datetime
    causal_cutoff: datetime


def _rule_result(row: EvaluationRow) -> UniverseResult:
    if row.slot_state is SlotState.CANDIDATE:
        return UniverseResult.ADMITTED
    return UniverseResult.EXCLUDED


def _reason_code(row: EvaluationRow) -> str:
    if row.slot_state is SlotState.CANDIDATE:
        return "CANDIDATE"
    reason = row.attrition_reason
    if not isinstance(reason, AttritionReason):
        raise ForwardEvaluationUniverseBindingError(
            "excluded evaluation row lacks canonical attrition reason"
        )
    return reason.value


def _member_expectation(
    *,
    protocol: ForwardEvidenceProtocolEnvelope,
    universe_sha256: str,
    membership_sha256: str,
    row: EvaluationRow,
) -> ForwardUniverseMemberExpectation:
    row_id = row.row_id
    opportunity_id = f"evaluation-universe-opportunity:{row_id}"
    source_receipt_id = f"evaluation-universe-receipt:{row_id}"
    result = _rule_result(row)
    reason = _reason_code(row)
    acquisition = row.slot_state.value
    observed_lower = _instant(row.source_at, "row source_at")
    observed_upper = _instant(row.committed_at, "row committed_at")
    receipt_sha256 = _digest(
        {
            "rule_id": FORWARD_UNIVERSE_RULE_ID,
            "rule_sha256": FORWARD_UNIVERSE_RULE_SHA256,
            "forward_protocol_sha256": protocol.protocol_sha256,
            "campaign_id": protocol.campaign_id,
            "universe_sha256": universe_sha256,
            "membership_sha256": membership_sha256,
            "row_id": row_id,
            "row": row.to_payload(),
            "universe_rule_result": result.value,
            "universe_rule_reason_code": reason,
            "provider_acquisition_state": acquisition,
        }
    )
    return ForwardUniverseMemberExpectation(
        row_id=row_id,
        opportunity_id=opportunity_id,
        source_receipt_id=source_receipt_id,
        source_receipt_sha256=receipt_sha256,
        universe_rule_result=result,
        universe_rule_reason_code=reason,
        provider_acquisition_state=acquisition,
        observed_lower=observed_lower,
        observed_upper=observed_upper,
        causal_cutoff=observed_upper,
    )


def _load_expectations(
    *,
    store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
) -> tuple[tuple[ForwardUniverseMemberExpectation, ...], tuple[str, str]]:
    if type(store) is not ProviderEvaluationUniverseStore:
        raise TypeError("store must be exact ProviderEvaluationUniverseStore")
    if type(protocol) is not ForwardEvidenceProtocolEnvelope:
        raise TypeError("protocol must be exact ForwardEvidenceProtocolEnvelope")
    if (
        protocol.candidate_universe_rule_id != FORWARD_UNIVERSE_RULE_ID
        or protocol.candidate_universe_rule_sha256 != FORWARD_UNIVERSE_RULE_SHA256
    ):
        raise ForwardEvaluationUniverseBindingError(
            "forward protocol does not precommit the canonical provider evaluation-universe rule"
        )

    ledger = _CANONICAL_PROVIDER_UNIVERSE_LOAD(store)
    if ledger is None:
        raise ForwardEvaluationUniverseBindingError(
            "canonical provider evaluation universe is not durably available"
        )
    universe = ledger.universe
    if protocol.campaign_id != universe.campaign_id:
        raise ForwardEvaluationUniverseBindingError(
            "forward campaign does not match durable evaluation-universe campaign"
        )
    if protocol.scientific_protocol_sha256 != universe.protocol_sha256:
        raise ForwardEvaluationUniverseBindingError(
            "forward scientific protocol does not match durable evaluation-universe protocol"
        )

    expectations = tuple(
        sorted(
            (
                _member_expectation(
                    protocol=protocol,
                    universe_sha256=universe.universe_sha256,
                    membership_sha256=universe.membership_sha256,
                    row=row,
                )
                for row in universe.rows
            ),
            key=lambda item: item.source_receipt_id,
        )
    )
    if not expectations:
        raise ForwardEvaluationUniverseBindingError(
            "durable evaluation universe contains no source members"
        )
    earliest = min(item.observed_lower for item in expectations)
    if protocol.precommit_anchor_upper >= earliest:
        raise ForwardEvaluationUniverseBindingError(
            "forward protocol must be anchored before the first authoritative source observation"
        )
    return expectations, (universe.universe_sha256, universe.membership_sha256)


def resolve_forward_universe_members(
    *,
    store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
) -> tuple[ForwardUniverseMemberExpectation, ...]:
    """Project the exact product-owned source universe for forward opportunity creation."""

    expectations, _identity = _load_expectations(store=store, protocol=protocol)
    return expectations


def authorize_forward_source_receipts(
    *,
    store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
    opportunities: Sequence[ForwardOpportunityEnvelope],
) -> tuple[AuthoritativeSourceReceipt, ...]:
    """Re-resolve durable membership and authorize only its exact forward projection.

    This function is intentionally fail-closed and does not accept a caller-supplied
    inventory of authoritative receipts.  Every receipt is regenerated from the
    durable product universe after exact opportunity coverage is verified.
    """

    expectations, identity_before = _load_expectations(store=store, protocol=protocol)
    expected_by_receipt = {item.source_receipt_id: item for item in expectations}

    materialized = tuple(opportunities)
    if not all(type(item) is ForwardOpportunityEnvelope for item in materialized):
        raise TypeError("opportunities must contain exact ForwardOpportunityEnvelope values")
    by_receipt: dict[str, ForwardOpportunityEnvelope] = {}
    opportunity_ids: set[str] = set()
    for item in materialized:
        if item.source_receipt_id in by_receipt:
            raise ForwardEvaluationUniverseBindingError(
                "forward opportunities duplicate an authoritative source receipt"
            )
        if item.opportunity_id in opportunity_ids:
            raise ForwardEvaluationUniverseBindingError(
                "forward opportunities duplicate an opportunity identity"
            )
        by_receipt[item.source_receipt_id] = item
        opportunity_ids.add(item.opportunity_id)

    if set(by_receipt) != set(expected_by_receipt):
        missing = sorted(set(expected_by_receipt) - set(by_receipt))
        invented = sorted(set(by_receipt) - set(expected_by_receipt))
        raise ForwardEvaluationUniverseBindingError(
            "forward opportunity inventory does not equal durable source membership "
            f"(missing={missing!r}, invented={invented!r})"
        )

    receipts: list[AuthoritativeSourceReceipt] = []
    for receipt_id in sorted(expected_by_receipt):
        expected = expected_by_receipt[receipt_id]
        item = by_receipt[receipt_id]
        if (
            item.campaign_id != protocol.campaign_id
            or item.protocol_sha256 != protocol.protocol_sha256
            or item.runtime_identity_sha256 != protocol.runtime_identity_sha256
        ):
            raise ForwardEvaluationUniverseBindingError(
                "forward opportunity campaign/protocol/runtime identity drifted from precommit"
            )
        actual = (
            item.opportunity_id,
            item.source_receipt_sha256,
            item.universe_rule_result,
            item.universe_rule_reason_code,
            item.provider_acquisition_state,
            item.observed_lower,
            item.observed_upper,
            item.causal_cutoff,
        )
        required = (
            expected.opportunity_id,
            expected.source_receipt_sha256,
            expected.universe_rule_result,
            expected.universe_rule_reason_code,
            expected.provider_acquisition_state,
            expected.observed_lower,
            expected.observed_upper,
            expected.causal_cutoff,
        )
        if actual != required:
            raise ForwardEvaluationUniverseBindingError(
                "forward opportunity does not exactly match durable source-universe projection"
            )
        receipts.append(
            AuthoritativeSourceReceipt(
                receipt_id=expected.source_receipt_id,
                receipt_sha256=expected.source_receipt_sha256,
                campaign_id=protocol.campaign_id,
                opportunity_id=expected.opportunity_id,
                universe_rule_result=expected.universe_rule_result,
            )
        )

    _expectations_after, identity_after = _load_expectations(store=store, protocol=protocol)
    if identity_after != identity_before:
        raise ForwardEvaluationUniverseBindingError(
            "durable source-universe identity changed during forward receipt resolution"
        )
    return tuple(receipts)


__all__ = [
    "FORWARD_UNIVERSE_RULE_ID",
    "FORWARD_UNIVERSE_RULE_SHA256",
    "ForwardEvaluationUniverseBindingError",
    "ForwardUniverseMemberExpectation",
    "authorize_forward_source_receipts",
    "resolve_forward_universe_members",
]

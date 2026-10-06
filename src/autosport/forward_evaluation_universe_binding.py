from __future__ import annotations

"""Bind forward source receipts to prospective + durable product authority.

Positive membership is never accepted from caller-created receipts or from whichever
ProviderEvaluationUniverseStore happens to be supplied.  The resolver composes two
existing authorities:

1. #1257 CampaignPrecommitManifest + monotonic publication witness fixes the
   prospective campaign/evaluation-plan identity before source observation; and
2. ProviderEvaluationUniverseStore re-resolves the exact realized durable rows,
   universe digest, and membership digest after provider observation, with guarded
   backing identity/load semantics.

No second universe store, scheduler, execution authority, or money permission exists
here.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from ._forward_universe_backing_guard import (
    ForwardUniverseBackingGuardError,
    load_guarded_provider_evaluation_universe,
)
from .evaluation_universe import AttritionReason, EvaluationRow, SlotState
from .forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    UniverseResult,
)
from .forward_universe_precommit_authority import (
    ForwardUniversePrecommitAuthorityError,
    ForwardUniversePrecommitLocator,
    resolve_forward_universe_precommit_authority,
)
from .provider_evaluation_universe import ProviderEvaluationUniverseStore


# Kept inspectable for compatibility/debugging.  Positive authority captures the exact
# executable into a closure and fails closed if its code is changed in place.
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
        raise ForwardEvaluationUniverseBindingError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardEvaluationUniverseBindingError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


_RULE_PAYLOAD = {
    "rule_id": FORWARD_UNIVERSE_RULE_ID,
    "authority": "CampaignPrecommitManifest + ProviderEvaluationUniverseStore",
    "membership": "exact realized durable EvaluationUniverse rows",
    "expected_universe_authority": (
        "CampaignPrecommitPublicationWitness + prospective evaluation-plan identity"
    ),
    "backing_authority": (
        "workspace identity + monotonic evaluation-universe namespace locator"
    ),
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
    "receipt_payload": (
        "forward protocol + prospective precommit authority + durable backing locator + "
        "immutable universe + full canonical EvaluationRow"
    ),
}
FORWARD_UNIVERSE_RULE_SHA256 = _digest(_RULE_PAYLOAD)


@dataclass(frozen=True, slots=True)
class ForwardUniverseMemberExpectation:
    """Deterministic projection; authority is established only by re-resolution."""

    row_id: str
    opportunity_id: str
    source_receipt_id: str
    source_receipt_sha256: str
    precommit_authority_sha256: str
    backing_locator_sha256: str
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
    precommit_authority_sha256: str,
    backing_locator_sha256: str,
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
            "precommit_authority_sha256": precommit_authority_sha256,
            "backing_locator_sha256": backing_locator_sha256,
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
        precommit_authority_sha256=precommit_authority_sha256,
        backing_locator_sha256=backing_locator_sha256,
        universe_rule_result=result,
        universe_rule_reason_code=reason,
        provider_acquisition_state=acquisition,
        observed_lower=observed_lower,
        observed_upper=observed_upper,
        causal_cutoff=observed_upper,
    )


def _build_load_expectations(
    *,
    store_type: type[ProviderEvaluationUniverseStore],
    precommit_locator_type: type[ForwardUniversePrecommitLocator],
    canonical_load,
    guarded_load,
    precommit_resolver,
    backing_error: type[BaseException],
    precommit_error: type[BaseException],
):
    """Capture every executable/read authority used by positive forward resolution."""

    canonical_load_code = canonical_load.__code__
    guarded_load_code = guarded_load.__code__
    precommit_resolver_code = precommit_resolver.__code__

    def load_expectations(
        *,
        store: ProviderEvaluationUniverseStore,
        protocol: ForwardEvidenceProtocolEnvelope,
        precommit: ForwardUniversePrecommitLocator,
    ) -> tuple[
        tuple[ForwardUniverseMemberExpectation, ...],
        tuple[str, str, str, str],
    ]:
        if type(store) is not store_type:
            raise TypeError("store must be exact ProviderEvaluationUniverseStore")
        if type(protocol) is not ForwardEvidenceProtocolEnvelope:
            raise TypeError("protocol must be exact ForwardEvidenceProtocolEnvelope")
        if type(precommit) is not precommit_locator_type:
            raise TypeError("precommit must be exact ForwardUniversePrecommitLocator")
        if (
            protocol.candidate_universe_rule_id != FORWARD_UNIVERSE_RULE_ID
            or protocol.candidate_universe_rule_sha256 != FORWARD_UNIVERSE_RULE_SHA256
        ):
            raise ForwardEvaluationUniverseBindingError(
                "forward protocol does not precommit the canonical provider evaluation-universe rule"
            )

        if canonical_load.__code__ is not canonical_load_code:
            raise ForwardEvaluationUniverseBindingError(
                "canonical provider evaluation-universe load authority changed"
            )
        if guarded_load.__code__ is not guarded_load_code:
            raise ForwardEvaluationUniverseBindingError(
                "guarded provider evaluation-universe load authority changed"
            )
        if precommit_resolver.__code__ is not precommit_resolver_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward universe precommit resolver authority changed"
            )
        try:
            ledger, backing_locator = guarded_load(store)
        except backing_error as exc:
            raise ForwardEvaluationUniverseBindingError(
                "provider evaluation-universe backing locator authority changed"
            ) from exc
        if canonical_load.__code__ is not canonical_load_code:
            raise ForwardEvaluationUniverseBindingError(
                "canonical provider evaluation-universe load authority changed"
            )
        if guarded_load.__code__ is not guarded_load_code:
            raise ForwardEvaluationUniverseBindingError(
                "guarded provider evaluation-universe load authority changed"
            )
        if precommit_resolver.__code__ is not precommit_resolver_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward universe precommit resolver authority changed"
            )
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
        if not universe.rows:
            raise ForwardEvaluationUniverseBindingError(
                "durable evaluation universe contains no source members"
            )

        earliest = min(_instant(row.source_at, "row source_at") for row in universe.rows)
        latest = max(_instant(row.committed_at, "row committed_at") for row in universe.rows)
        if protocol.precommit_anchor_upper >= earliest:
            raise ForwardEvaluationUniverseBindingError(
                "forward protocol must be anchored before the first authoritative source observation"
            )

        try:
            precommit_resolution = precommit_resolver(
                locator=precommit,
                campaign_id=protocol.campaign_id,
                source_id=store.source_id,
                earliest_source_observation=earliest,
                latest_source_observation=latest,
            )
        except precommit_error as exc:
            raise ForwardEvaluationUniverseBindingError(
                "prospective campaign precommit plan cannot be re-resolved"
            ) from exc
        if precommit_resolver.__code__ is not precommit_resolver_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward universe precommit resolver authority changed"
            )

        expectations = tuple(
            sorted(
                (
                    _member_expectation(
                        protocol=protocol,
                        precommit_authority_sha256=precommit_resolution.authority_sha256,
                        backing_locator_sha256=backing_locator.locator_sha256,
                        universe_sha256=universe.universe_sha256,
                        membership_sha256=universe.membership_sha256,
                        row=row,
                    )
                    for row in universe.rows
                ),
                key=lambda item: item.source_receipt_id,
            )
        )
        return expectations, (
            precommit_resolution.authority_sha256,
            backing_locator.locator_sha256,
            universe.universe_sha256,
            universe.membership_sha256,
        )

    return load_expectations


def _build_public_entrypoints(*, load_expectations):
    """Seal the positive resolver behind the exact composed authority callable."""

    load_expectations_code = load_expectations.__code__

    def _sealed_load_expectations(**kwargs):
        if load_expectations.__code__ is not load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe resolver authority changed"
            )
        result = load_expectations(**kwargs)
        if load_expectations.__code__ is not load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe resolver authority changed"
            )
        return result

    sealed_load_expectations_code = _sealed_load_expectations.__code__

    def resolve_forward_universe_members(
        *,
        store: ProviderEvaluationUniverseStore,
        protocol: ForwardEvidenceProtocolEnvelope,
        precommit: ForwardUniversePrecommitLocator,
    ) -> tuple[ForwardUniverseMemberExpectation, ...]:
        """Project only the exact prospectively selected durable source universe."""
    
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
        expectations, _identity = _sealed_load_expectations(
            store=store,
            protocol=protocol,
            precommit=precommit,
        )
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
        return expectations
    
    
    def authorize_forward_source_receipts(
        *,
        store: ProviderEvaluationUniverseStore,
        protocol: ForwardEvidenceProtocolEnvelope,
        precommit: ForwardUniversePrecommitLocator,
        opportunities: Sequence[ForwardOpportunityEnvelope],
    ) -> tuple[AuthoritativeSourceReceipt, ...]:
        """Re-resolve prospective + durable authority and authorize exact coverage only."""
    
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
        expectations, identity_before = _sealed_load_expectations(
            store=store,
            protocol=protocol,
            precommit=precommit,
        )
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
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
    
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
        _expectations_after, identity_after = _sealed_load_expectations(
            store=store,
            protocol=protocol,
            precommit=precommit,
        )
        if _sealed_load_expectations.__code__ is not sealed_load_expectations_code:
            raise ForwardEvaluationUniverseBindingError(
                "forward source-universe public resolver dispatch changed"
            )
        if identity_after != identity_before:
            raise ForwardEvaluationUniverseBindingError(
                "prospective or durable source-universe authority changed during receipt resolution"
            )
        return tuple(receipts)
    

    return resolve_forward_universe_members, authorize_forward_source_receipts


resolve_forward_universe_members, authorize_forward_source_receipts = _build_public_entrypoints(
    load_expectations=_build_load_expectations(
    store_type=ProviderEvaluationUniverseStore,
    precommit_locator_type=ForwardUniversePrecommitLocator,
    canonical_load=_CANONICAL_PROVIDER_UNIVERSE_LOAD,
    guarded_load=load_guarded_provider_evaluation_universe,
    precommit_resolver=resolve_forward_universe_precommit_authority,
    backing_error=ForwardUniverseBackingGuardError,
    precommit_error=ForwardUniversePrecommitAuthorityError,
),
)
del _build_load_expectations
del _build_public_entrypoints

__all__ = [
    "FORWARD_UNIVERSE_RULE_ID",
    "FORWARD_UNIVERSE_RULE_SHA256",
    "ForwardEvaluationUniverseBindingError",
    "ForwardUniverseMemberExpectation",
    "ForwardUniversePrecommitLocator",
    "authorize_forward_source_receipts",
    "resolve_forward_universe_members",
]

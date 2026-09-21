from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .market_outcomes import MarketSettlementOutcomeAuthority


class OutcomeRevisionRelation(str, Enum):
    """Relationship between two independently verified outcome authorities."""

    SAME_REVISION = "same_revision"
    STRICT_SUCCESSOR = "strict_successor"


@dataclass(frozen=True, slots=True)
class MarketOutcomeRevisionDecision:
    """Deterministic result of a fail-closed outcome-authority lineage check.

    The object does not mint or widen outcome authority. It only records why an
    already-verified candidate may replace an already-verified predecessor.
    """

    relation: OutcomeRevisionRelation
    previous_authority_sha256: str
    current_authority_sha256: str
    previous_source_revision: str
    current_source_revision: str
    previous_causal_cutoff: str
    current_causal_cutoff: str
    previous_observed_at: str
    current_observed_at: str


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("outcome authority timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def validate_market_outcome_authority_revision(
    previous: MarketSettlementOutcomeAuthority,
    current: MarketSettlementOutcomeAuthority,
) -> MarketOutcomeRevisionDecision:
    """Validate a replacement inside one exact provider/market authority lineage.

    Both inputs must already be canonical verified authorities. This function
    prevents a cache/store/restart consumer from treating an older or ambiguous
    authority as the current replacement merely because each object is valid in
    isolation.

    A repeated observation of the same provider revision is idempotent when its
    causal revision/provenance and settlement protocol are unchanged and its local
    observation time does not move backwards. A genuine replacement must advance
    the causal cutoff strictly. Same-cutoff disagreements, causal rollback,
    observation rollback, identity drift, and settlement/protocol drift fail closed.
    """

    if not isinstance(previous, MarketSettlementOutcomeAuthority):
        raise TypeError("previous must be a MarketSettlementOutcomeAuthority")
    if not isinstance(current, MarketSettlementOutcomeAuthority):
        raise TypeError("current must be a MarketSettlementOutcomeAuthority")

    if current.identity != previous.identity:
        raise ValueError("market outcome revision lineage identity mismatch")
    if current.roster_basis is not previous.roster_basis:
        raise ValueError("market outcome revision roster basis changed")
    if current.settlement_semantics is not previous.settlement_semantics:
        raise ValueError("market outcome revision settlement semantics changed")
    if current.settlement_rules_sha256 != previous.settlement_rules_sha256:
        raise ValueError("market outcome revision settlement rules changed")
    if current.verification_protocol_sha256 != previous.verification_protocol_sha256:
        raise ValueError("market outcome revision verification protocol changed")

    previous_cutoff = _utc_timestamp(previous.causal_cutoff)
    current_cutoff = _utc_timestamp(current.causal_cutoff)
    previous_observed = _utc_timestamp(previous.observed_at)
    current_observed = _utc_timestamp(current.observed_at)

    if current_observed < previous_observed:
        raise ValueError("market outcome revision observed_at moved backwards")
    if current_cutoff < previous_cutoff:
        raise ValueError("market outcome revision causal cutoff rollback")

    if current_cutoff == previous_cutoff:
        same_provider_revision = (
            current.source_revision == previous.source_revision
            and current.selection_ids == previous.selection_ids
            and current.roster_provenance_sha256
            == previous.roster_provenance_sha256
        )
        if not same_provider_revision:
            raise ValueError(
                "conflicting market outcome evidence at identical causal cutoff"
            )
        relation = OutcomeRevisionRelation.SAME_REVISION
    else:
        relation = OutcomeRevisionRelation.STRICT_SUCCESSOR

    return MarketOutcomeRevisionDecision(
        relation=relation,
        previous_authority_sha256=previous.authority_sha256,
        current_authority_sha256=current.authority_sha256,
        previous_source_revision=previous.source_revision,
        current_source_revision=current.source_revision,
        previous_causal_cutoff=previous.causal_cutoff,
        current_causal_cutoff=current.causal_cutoff,
        previous_observed_at=previous.observed_at,
        current_observed_at=current.observed_at,
    )

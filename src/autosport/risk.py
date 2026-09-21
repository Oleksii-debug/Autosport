from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from . import _risk_core as _core
from .risk_of_ruin_authority import verify_risk_of_ruin_authority

# Preserve the historical public/private import surface while keeping the mature
# risk engine byte-for-byte unchanged in _risk_core.  Only PaperRiskPolicy is
# replaced below with the narrow product-issued risk-of-ruin authority seam.
for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


@dataclass(frozen=True, slots=True)
class PaperRiskPolicy(_core.PaperRiskPolicy):
    """Canonical paper risk policy with durable risk-of-ruin result authority."""

    risk_of_ruin_registry_path: str | Path | None = None

    def __post_init__(self) -> None:
        _core.PaperRiskPolicy.__post_init__(self)
        raw_path = self.risk_of_ruin_registry_path
        if raw_path is None:
            return
        if not isinstance(raw_path, (str, Path)):
            raise TypeError("risk_of_ruin_registry_path must be str, Path or None")
        canonical = str(raw_path)
        if not canonical or canonical != canonical.strip():
            raise ValueError("risk_of_ruin_registry_path must be a non-empty canonical path")
        object.__setattr__(self, "risk_of_ruin_registry_path", canonical)

    def provenance_payload(self) -> dict[str, object]:
        payload = dict(_core.PaperRiskPolicy.provenance_payload(self))
        if self.risk_of_ruin_registry_path is not None:
            payload["schema_version"] = 2
            payload["risk_of_ruin_authority"] = {
                "kind": "scientific_registry_evaluation_bundle_v1",
                "registry_path": str(self.risk_of_ruin_registry_path),
            }
        return payload

    def _risk_of_ruin_evidence_decision(
        self,
        book: PaperBook,
        amount: Decimal,
        goal: EconomicGoalContract,
        context: ProposedTicketRiskContext,
    ) -> RiskDecision | None:
        base_decision = _core.PaperRiskPolicy._risk_of_ruin_evidence_decision(
            book,
            amount,
            goal,
            context,
        )
        if base_decision is not None or goal.max_risk_of_ruin >= Decimal("1"):
            return base_decision
        evidence = context.risk_of_ruin_evidence
        assert evidence is not None
        assert context.proposal_ts is not None
        verified, reason = verify_risk_of_ruin_authority(
            self.risk_of_ruin_registry_path,
            evidence,
            kind="single",
            available_by=context.proposal_ts,
        )
        return None if verified else RiskDecision(False, reason)

    def _risk_of_ruin_vector_evidence_decision(
        self,
        book: PaperBook,
        goal: EconomicGoalContract,
        contexts: tuple[ProposedTicketRiskContext, ...],
        stakes: tuple[Decimal, ...],
        evidence: RiskOfRuinVectorEvidence | None,
    ) -> RiskDecision | None:
        base_decision = _core.PaperRiskPolicy._risk_of_ruin_vector_evidence_decision(
            book,
            goal,
            contexts,
            stakes,
            evidence,
        )
        if base_decision is not None or goal.max_risk_of_ruin >= Decimal("1"):
            return base_decision
        assert evidence is not None
        proposal_times = [
            _core._canonical_context_timestamp("proposal_ts", context.proposal_ts)[1]
            for context in contexts
            if context.proposal_ts is not None
        ]
        if len(proposal_times) != len(contexts):
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence lacks canonical proposal time",
            )
        available_by = min(proposal_times).astimezone(timezone.utc).isoformat()
        verified, reason = verify_risk_of_ruin_authority(
            self.risk_of_ruin_registry_path,
            evidence,
            kind="vector",
            available_by=available_by,
        )
        return None if verified else RiskDecision(False, reason)


# New objects must retain the historical public module identity for serialization
# and introspection.  Other exported objects remain the exact _risk_core objects.
PaperRiskPolicy.__module__ = __name__

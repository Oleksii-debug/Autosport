from __future__ import annotations

"""Bind exact sport-memory matchup evidence into normal opportunity evidence."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from .portfolio_plan import OpportunityEvidence
from .sport_memory_runtime import (
    SportMemoryError,
    SportMemoryMatchupEvidence,
    SportMemoryRuntime,
)


def _instant(name: str, value: object) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise SportMemoryError(f"{name} must be a canonical timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SportMemoryError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SportMemoryError(f"{name} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def bind_sport_memory_to_opportunity_evidence(
    base: OpportunityEvidence,
    matchup: SportMemoryMatchupEvidence,
    *,
    runtime: SportMemoryRuntime,
) -> OpportunityEvidence:
    """Return normal OpportunityEvidence cryptographically bound to sport memory.

    The binder preserves the base evidence truth/execution semantics. It only
    strengthens reproducibility and causal provenance by committing the exact
    subject/opponent immutable memory identities selected before observed_at.
    """
    if not isinstance(base, OpportunityEvidence):
        raise TypeError("base must be OpportunityEvidence")
    if not isinstance(runtime, SportMemoryRuntime):
        raise TypeError("runtime must be SportMemoryRuntime")
    from .sport_memory_checkpoint import BoundSportMemoryRuntime

    if type(runtime) is not BoundSportMemoryRuntime:
        raise SportMemoryError(
            "opportunity evidence binding requires canonical bound sport-memory runtime"
        )
    matchup = runtime.verify_matchup_evidence(matchup)

    observed = _instant("base observed_at", base.observed_at)
    matchup_as_of = _instant("matchup as_of", matchup.as_of)
    matchup_published = _instant("matchup published_at", matchup.published_at)
    matchup_cutoff = _instant("matchup causal_cutoff", matchup.causal_cutoff)
    base_cutoff = _instant("base causal_cutoff", base.causal_cutoff)
    if matchup_as_of != observed:
        raise SportMemoryError(
            "sport-memory matchup as_of must exactly match opportunity evidence observed_at"
        )
    if matchup_published > observed:
        raise SportMemoryError(
            "sport-memory matchup was not published by opportunity evidence time"
        )
    combined_cutoff = max(base_cutoff, matchup_cutoff)
    if combined_cutoff > observed:
        raise SportMemoryError(
            "sport-memory causal cutoff exceeds opportunity evidence time"
        )

    binding_sha256 = _digest(
        {
            "schema": "autosport.sport_memory_opportunity_binding",
            "schema_version": 1,
            "base_evidence_sha256": base.evidence_sha256,
            "sport_memory_matchup_id": matchup.matchup_id,
        }
    )
    return OpportunityEvidence(
        evidence_id=f"sport-memory-bound:{binding_sha256}",
        observed_at=base.observed_at,
        causal_cutoff=(
            matchup.causal_cutoff
            if matchup_cutoff >= base_cutoff
            else base.causal_cutoff
        ),
        reproducibility_sha256=binding_sha256,
        truth=base.truth,
        outcome_space_complete=base.outcome_space_complete,
        terminal_state_space_sha256=base.terminal_state_space_sha256,
        execution_assumptions_sha256=base.execution_assumptions_sha256,
        execution_feasible=base.execution_feasible,
    )


__all__ = ["bind_sport_memory_to_opportunity_evidence"]

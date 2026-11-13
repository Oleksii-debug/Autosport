"""Fail-honest operator read model for the incident/model-risk register.

This module is presentation-only. It consumes the canonical durable
IncidentRiskStore and canonical operator projection, never grants execution,
risk, provider, scientific, recovery, or release authority, and intentionally
does not expose raw store/exception diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .incident_risk_register import (
    IncidentRiskRegisterError,
    OperatorRiskProjection,
    operator_projection,
    operator_sort,
)
from .incident_risk_store import IncidentRiskStore, IncidentRiskStoreError


@dataclass(frozen=True, slots=True)
class IncidentRiskOperatorRow:
    """One safe current operator row plus its product-availability time."""

    projection: OperatorRiskProjection
    known_at: str


@dataclass(frozen=True, slots=True)
class IncidentRiskOperatorView:
    """Bounded operator-facing register state.

    evidence_available distinguishes a verified empty register from an
    unavailable/corrupt register. state_key is localization-ready and does
    not contain a raw diagnostic.
    """

    evidence_available: bool
    state_key: str
    rows: tuple[IncidentRiskOperatorRow, ...]


def _unavailable_view() -> IncidentRiskOperatorView:
    return IncidentRiskOperatorView(
        evidence_available=False,
        state_key="ui.risk_register.state.unavailable",
        rows=(),
    )


def load_incident_risk_operator_view(
    store: IncidentRiskStore,
) -> IncidentRiskOperatorView:
    """Load current durable incident truth into a fail-honest safe read model."""

    if type(store) is not IncidentRiskStore:
        raise TypeError(
            "incident risk operator view requires an exact IncidentRiskStore"
        )

    try:
        snapshot = store.load()
        availability_by_revision = {
            (entry_id, revision): available_at
            for entry_id, revision, available_at in snapshot.availability
        }
        ordered = operator_sort(snapshot.current_entries)
        rows = tuple(
            IncidentRiskOperatorRow(
                projection=operator_projection(entry),
                known_at=availability_by_revision[
                    (entry.entry_id, entry.revision)
                ],
            )
            for entry in ordered
        )
    except (IncidentRiskStoreError, IncidentRiskRegisterError, OSError):
        return _unavailable_view()

    return IncidentRiskOperatorView(
        evidence_available=True,
        state_key=(
            "ui.risk_register.state.empty"
            if not rows
            else "ui.risk_register.state.available"
        ),
        rows=rows,
    )

"""Regression for sport-specific market semantics surviving into ticket identity.

This is intentionally a structural TEST-only falsifier for the #1193/#1223
multi-sport conformance lineage.  It does not define a second market identity
model.  The canonical MarketEvent already owns ``market_semantics_id``; a
TicketLeg that cannot represent that identity can silently collapse different
sport/market rule families after ticket construction or persistence.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "autosport"


def _annotated_fields(class_name: str) -> tuple[Path, set[str]]:
    matches: list[tuple[Path, ast.ClassDef]] = []

    for path in sorted(_SOURCE_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                matches.append((path, node))

    assert len(matches) == 1, (
        f"expected exactly one canonical {class_name} definition under "
        f"{_SOURCE_ROOT}, found {[str(path) for path, _ in matches]}"
    )

    path, class_node = matches[0]
    fields = {
        statement.target.id
        for statement in class_node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    }
    return path, fields


def test_market_event_has_canonical_market_semantics_identity() -> None:
    """Control: the upstream generic market event already carries this identity."""

    path, fields = _annotated_fields("MarketEvent")
    assert "market_semantics_id" in fields, (
        "#1193 requires explicit sport/market semantic identity, but canonical "
        f"MarketEvent in {path} no longer exposes market_semantics_id"
    )


def test_ticket_leg_can_preserve_market_semantics_identity() -> None:
    """A ticket leg must be able to retain the exact market-rule identity."""

    path, fields = _annotated_fields("TicketLeg")
    assert "market_semantics_id" in fields, (
        "TicketLeg drops canonical MarketEvent.market_semantics_id.  That makes "
        "a second sport's market-rule identity unrepresentable after ticket "
        "construction/persistence and can alias same-labelled markets with "
        f"different sport semantics.  Canonical TicketLeg: {path}"
    )

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosport.incident_risk_operator import load_incident_risk_operator_view
from autosport.incident_risk_register import (
    IncidentRiskEntry,
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
    derive_occurrence_entry_id,
)
from autosport.incident_risk_store import IncidentRiskStore


class IncidentRiskOperatorViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.authority_temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.authority_root = Path(self.authority_temp.name)
        self.store = IncidentRiskStore(
            self.workspace,
            authority_root=self.authority_root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.authority_temp.cleanup()

    @staticmethod
    def _entry(
        tag: str,
        *,
        severity: RiskSeverity = RiskSeverity.HIGH,
        requires_operator_action: bool = True,
        title: str = "Provider incident",
    ) -> IncidentRiskEntry:
        occurrence = f"evidence://incident/{tag}"
        components = ("market_mirror",)
        return IncidentRiskEntry(
            entry_id=derive_occurrence_entry_id(
                kind=RegisterEntryKind.INCIDENT,
                affected_components=components,
                occurrence_evidence_refs=(occurrence,),
            ),
            revision=1,
            kind=RegisterEntryKind.INCIDENT,
            severity=severity,
            status=RiskStatus.OPEN,
            evidence_state=RiskEvidenceState.PARTIAL,
            opened_at="2026-10-06T05:00:00+00:00",
            updated_at="2026-10-06T05:01:00+00:00",
            title=title,
            summary="Operator-safe incident summary.",
            affected_components=components,
            occurrence_evidence_refs=(occurrence,),
            evidence_refs=(occurrence,),
            requires_operator_action=requires_operator_action,
        )

    def test_verified_empty_store_is_distinct_from_unavailable(self) -> None:
        view = load_incident_risk_operator_view(self.store)

        self.assertTrue(view.evidence_available)
        self.assertEqual(view.state_key, "ui.risk_register.state.empty")
        self.assertEqual(view.rows, ())

    def test_current_view_preserves_product_known_at_and_safe_projection(self) -> None:
        entry = self._entry(
            "secret",
            title="Authorization: Bearer operator-secret-value",
        )
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            return_value="2026-10-06T05:02:00+00:00",
        ):
            self.store.append(entry)

        view = load_incident_risk_operator_view(self.store)

        self.assertTrue(view.evidence_available)
        self.assertEqual(view.state_key, "ui.risk_register.state.available")
        self.assertEqual(len(view.rows), 1)
        row = view.rows[0]
        self.assertEqual(row.known_at, "2026-10-06T05:02:00+00:00")
        self.assertEqual(row.projection.opened_at, entry.opened_at)
        self.assertEqual(row.projection.updated_at, entry.updated_at)
        self.assertEqual(
            row.projection.next_action_key,
            "ui.risk_register.next_action.review",
        )
        self.assertNotIn("operator-secret-value", repr(view))
        self.assertIn("[REDACTED]", row.projection.title)

    def test_view_uses_canonical_operator_sort_order(self) -> None:
        low_action = self._entry(
            "low",
            severity=RiskSeverity.LOW,
            requires_operator_action=True,
        )
        high_monitor = self._entry(
            "high",
            severity=RiskSeverity.HIGH,
            requires_operator_action=False,
        )
        with mock.patch(
            "autosport.incident_risk_store._availability_now",
            side_effect=(
                "2026-10-06T05:02:00+00:00",
                "2026-10-06T05:03:00+00:00",
            ),
        ):
            self.store.append(high_monitor)
            self.store.append(low_action)

        view = load_incident_risk_operator_view(self.store)

        self.assertEqual(
            tuple(row.projection.entry_id for row in view.rows),
            (low_action.entry_id, high_monitor.entry_id),
        )

    def test_corrupt_store_is_unavailable_not_false_empty(self) -> None:
        self.store.append(self._entry("corrupt"))
        self.store.path.write_text(
            '{"secret":"provider-token-must-not-surface"',
            encoding="utf-8",
        )

        view = load_incident_risk_operator_view(self.store)

        self.assertFalse(view.evidence_available)
        self.assertEqual(view.state_key, "ui.risk_register.state.unavailable")
        self.assertEqual(view.rows, ())
        self.assertNotIn("provider-token-must-not-surface", repr(view))

    def test_deleted_committed_store_is_unavailable_not_false_empty(self) -> None:
        self.store.append(self._entry("deleted"))
        self.store.path.unlink()

        view = load_incident_risk_operator_view(
            IncidentRiskStore(
                self.workspace,
                authority_root=self.authority_root,
            )
        )

        self.assertFalse(view.evidence_available)
        self.assertEqual(view.state_key, "ui.risk_register.state.unavailable")
        self.assertEqual(view.rows, ())

    def test_operator_view_has_no_financial_or_release_authority_fields(self) -> None:
        view = load_incident_risk_operator_view(self.store)
        rendered = repr(view)
        for forbidden in (
            "real_money_execution",
            "execution_authorized",
            "risk_authorized",
            "human_tested",
            "nvda_verified",
            "whole_product_complete",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()

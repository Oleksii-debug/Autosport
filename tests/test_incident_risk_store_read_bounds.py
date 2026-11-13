from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autosport.incident_risk_store as incident_risk_store
from autosport.incident_risk_store import IncidentRiskStoreError


class IncidentRiskStoreReadBoundTests(unittest.TestCase):
    def test_growth_after_initial_stat_is_bounded_before_full_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "incident_model_risk_store.json"
            path.write_bytes(b"{}")
            real_read = os.read
            grew = False

            def growing_read(descriptor: int, size: int) -> bytes:
                nonlocal grew
                if not grew:
                    grew = True
                    with path.open("ab") as handle:
                        handle.write(b"x" * 64)
                return real_read(descriptor, size)

            with (
                mock.patch.object(incident_risk_store, "_MAX_STORE_BYTES", 8),
                mock.patch.object(
                    incident_risk_store.os,
                    "read",
                    side_effect=growing_read,
                ),
            ):
                with self.assertRaisesRegex(
                    IncidentRiskStoreError,
                    "exceeds resource limit",
                ):
                    incident_risk_store._read_stable_store_text(path)

            self.assertTrue(grew)


if __name__ == "__main__":
    unittest.main()

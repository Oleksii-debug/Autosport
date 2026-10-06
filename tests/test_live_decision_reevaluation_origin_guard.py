from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.live_decision_reevaluation import resolve_live_decision_disposition


_DISPOSITION_ID = "a" * 64
_FORGED_CODE_CALLED = False


def _forged_verified_records(self):
    global _FORGED_CODE_CALLED
    _FORGED_CODE_CALLED = True
    raise AssertionError("in-place forged reader code must never execute")


class LiveDecisionReevaluationOriginGuardTests(unittest.TestCase):
    def test_ledger_subclass_cannot_forge_predecessor_read(self) -> None:
        called = False

        class ForgedLedger(JsonlDecisionLedger):
            def verified_records(self):
                nonlocal called
                called = True
                raise AssertionError("forged predecessor reader must never execute")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = ForgedLedger(Path(tmp) / "decisions.jsonl")
            with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                resolve_live_decision_disposition(_DISPOSITION_ID, ledger=ledger)
        self.assertFalse(called)

    def test_exact_ledger_instance_cannot_shadow_predecessor_read(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")

            def forged():
                nonlocal called
                called = True
                raise AssertionError("shadowed predecessor reader must never execute")

            ledger.verified_records = forged  # type: ignore[method-assign]
            with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                resolve_live_decision_disposition(_DISPOSITION_ID, ledger=ledger)
        self.assertFalse(called)

    def test_exact_ledger_instance_cannot_shadow_append_authority(self) -> None:
        called = False
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")

            def forged(_record):
                nonlocal called
                called = True
                raise AssertionError("shadowed append must never become product authority")

            ledger.append = forged  # type: ignore[method-assign]
            # The shared ledger-origin fence is evaluated before any durable read or
            # write. Resolving a missing predecessor is enough to prove the write
            # surface cannot be shadowed and then reused by persistence.
            with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                resolve_live_decision_disposition(_DISPOSITION_ID, ledger=ledger)
        self.assertFalse(called)

    def test_in_place_ledger_code_replacement_is_rejected_before_dispatch(self) -> None:
        global _FORGED_CODE_CALLED
        _FORGED_CODE_CALLED = False
        original_code = JsonlDecisionLedger.verified_records.__code__
        try:
            JsonlDecisionLedger.verified_records.__code__ = _forged_verified_records.__code__
            with tempfile.TemporaryDirectory() as tmp:
                ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
                with self.assertRaisesRegex(TypeError, "canonical JsonlDecisionLedger"):
                    resolve_live_decision_disposition(_DISPOSITION_ID, ledger=ledger)
            self.assertFalse(_FORGED_CODE_CALLED)
        finally:
            JsonlDecisionLedger.verified_records.__code__ = original_code


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.collector_retention as retention_module
from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_retention import (
    CollectorRetentionError,
    CollectorRetentionManager,
    RetentionPinKind,
)
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction


def make_delta(delta_id: str = "d1") -> CollectorDelta:
    payload = {
        "event_id": "source-x:event-1",
        "market_id": "source-x:winner",
        "selection_id": "source-x:player-a",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": "source-x",
        "sequence": 1,
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
        "sport": "table_tennis",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=MarketEvent.from_dict(payload).dedupe_key,
        event_id=payload["event_id"],
        source_payload_digest=digest_source_payload(raw),
        canonical_event_digest=canonical_event_digest(payload),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        revision_of=None,
        revision_number=0,
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


def make_owner_lifecycle(
    root: str,
    *,
    run_id: str = "run-7",
    decision_id: str = "decision-42",
    complete: bool,
) -> None:
    workspace = Path(root)
    registry = RunRegistry.initialize_pristine(workspace / "run_registry.json")
    book_path = workspace / "paper_book.json"
    PaperBook("10000").save(book_path)
    ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
    ledger.path.touch()

    market_sha256 = "a" * 64
    results_sha256 = "b" * 64
    strategy_id = "baseline-v1"
    base_book_sha256 = sha256_file(book_path)
    base_ledger_sha256 = sha256_file(ledger.path)
    key = registry.begin(
        market_sha256,
        results_sha256,
        strategy_id,
        run_id,
        base_paper_book_sha256=base_book_sha256,
        base_decision_ledger_sha256=base_ledger_sha256,
    )
    transaction = RunTransaction.start(
        workspace,
        run_id=run_id,
        experiment_key=key,
        market_sha256=market_sha256,
        results_sha256=results_sha256,
        strategy_id=strategy_id,
        base_paper_book_sha256=base_book_sha256,
        base_decision_ledger_sha256=base_ledger_sha256,
    )
    staged_ledger = JsonlDecisionLedger(transaction.run_ledger_path)
    staged_ledger.append(
        DecisionRecord(
            replay_run_id=run_id,
            agent="retention-owner-authority-test",
            observed_ts="2026-01-01T00:00:00+00:00",
            action="OBSERVE",
            payload={"fixture": "retention-owner-authority"},
            context_hash="retention-owner-authority-context",
            decision_id=decision_id,
            recorded_at="2026-01-01T00:00:01+00:00",
        )
    )
    if not complete:
        return

    transaction.stage_outputs(PaperBook("10001"), ledger.path)
    summary = transaction.precommit(
        {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "real_money_execution": False,
        }
    )
    summary_path = transaction.commit()
    registry.complete(
        key,
        str(summary_path),
        paper_book_sha256=str(summary["paper_book_sha256"]),
        decision_ledger_sha256=str(summary["decision_ledger_sha256"]),
    )
    transaction.mark_registry_completed()


class CollectorRetentionOwnerAuthorityTests(unittest.TestCase):
    def make_manager(self, root: str):
        collector = CollectorDeltaStore(Path(root) / "collector.sqlite")
        delta = make_delta()
        self.assertTrue(collector.append(delta))
        return collector, CollectorRetentionManager(collector), delta

    def pin_exists(
        self,
        collector: CollectorDeltaStore,
        *,
        kind: RetentionPinKind,
        owner_id: str,
        delta_id: str,
    ) -> bool:
        connection = collector._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM collector_retention_pins_v1 "
                "WHERE pin_kind=? AND owner_id=? AND delta_id=?",
                (kind.value, owner_id, delta_id),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def test_retention_module_run_registry_rebind_cannot_release_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, manager, delta = self.make_manager(tmp)
            make_owner_lifecycle(tmp, complete=False)
            manager.pin(
                kind=RetentionPinKind.REPLAY,
                owner_id="replay:run-7",
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                created_at="2026-01-01T00:00:07+00:00",
            )

            attacker_calls = []

            class ForgedRunRegistry:
                def __init__(self, path):
                    attacker_calls.append(("init", str(path)))

                def verified_completed_summary_for_run(self, run_id):
                    attacker_calls.append(("completed", run_id))
                    return ({"decision_ledger_sha256": "0" * 64}, "1" * 64)

            with patch.object(retention_module, "RunRegistry", ForgedRunRegistry):
                with self.assertRaisesRegex(
                    CollectorRetentionError,
                    "verified terminal lifecycle authority",
                ):
                    manager.release_pin(
                        kind=RetentionPinKind.REPLAY,
                        owner_id="replay:run-7",
                        delta_id=delta.delta_id,
                        canonical_event_digest=delta.canonical_event_digest,
                    )

            self.assertEqual(attacker_calls, [])
            self.assertTrue(
                self.pin_exists(
                    collector,
                    kind=RetentionPinKind.REPLAY,
                    owner_id="replay:run-7",
                    delta_id=delta.delta_id,
                )
            )

    def test_retention_module_mirrors_cannot_redirect_terminal_replay_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, manager, delta = self.make_manager(tmp)
            make_owner_lifecycle(tmp, complete=True)
            manager.pin(
                kind=RetentionPinKind.REPLAY,
                owner_id="replay:run-7",
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                created_at="2026-01-01T00:00:07+00:00",
            )

            attacker_calls = []

            class ForgedRunRegistry:
                def __init__(self, path):
                    attacker_calls.append(("registry", str(path)))

            class ForgedDecisionLedger:
                def __init__(self, path):
                    attacker_calls.append(("ledger", str(path)))

            with (
                patch.object(retention_module, "RunRegistry", ForgedRunRegistry),
                patch.object(
                    retention_module,
                    "JsonlDecisionLedger",
                    ForgedDecisionLedger,
                ),
            ):
                self.assertTrue(
                    manager.release_pin(
                        kind=RetentionPinKind.REPLAY,
                        owner_id="replay:run-7",
                        delta_id=delta.delta_id,
                        canonical_event_digest=delta.canonical_event_digest,
                    )
                )

            self.assertEqual(attacker_calls, [])
            restarted = CollectorRetentionManager(
                CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            )
            self.assertFalse(
                restarted.release_pin(
                    kind=RetentionPinKind.REPLAY,
                    owner_id="replay:run-7",
                    delta_id=delta.delta_id,
                    canonical_event_digest=delta.canonical_event_digest,
                )
            )

    def test_retention_module_mirrors_cannot_redirect_terminal_decision_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, manager, delta = self.make_manager(tmp)
            make_owner_lifecycle(tmp, decision_id="decision-42", complete=True)
            manager.pin(
                kind=RetentionPinKind.DECISION,
                owner_id="decision:decision-42",
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                created_at="2026-01-01T00:00:07+00:00",
            )

            attacker_calls = []

            class ForgedRunRegistry:
                def __init__(self, path):
                    attacker_calls.append(("registry", str(path)))

            class ForgedDecisionLedger:
                def __init__(self, path):
                    attacker_calls.append(("ledger", str(path)))

            with (
                patch.object(retention_module, "RunRegistry", ForgedRunRegistry),
                patch.object(
                    retention_module,
                    "JsonlDecisionLedger",
                    ForgedDecisionLedger,
                ),
            ):
                self.assertTrue(
                    manager.release_pin(
                        kind=RetentionPinKind.DECISION,
                        owner_id="decision:decision-42",
                        delta_id=delta.delta_id,
                        canonical_event_digest=delta.canonical_event_digest,
                    )
                )

            self.assertEqual(attacker_calls, [])
            self.assertFalse(
                self.pin_exists(
                    collector,
                    kind=RetentionPinKind.DECISION,
                    owner_id="decision:decision-42",
                    delta_id=delta.delta_id,
                )
            )

    def test_public_authority_entry_method_rebinds_do_not_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            _collector, manager, delta = self.make_manager(tmp)
            make_owner_lifecycle(tmp, complete=True)
            manager.pin(
                kind=RetentionPinKind.DECISION,
                owner_id="decision:decision-42",
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                created_at="2026-01-01T00:00:07+00:00",
            )

            attacker_calls = []

            def forged_completed(self, run_id):
                attacker_calls.append(("completed", run_id))
                return ({"decision_ledger_sha256": "0" * 64}, "1" * 64)

            def forged_snapshot(self):
                attacker_calls.append(("snapshot", str(self.path)))
                raise AssertionError("forged decision-ledger snapshot executed")

            with (
                patch.object(
                    RunRegistry,
                    "verified_completed_summary_for_run",
                    forged_completed,
                ),
                patch.object(
                    JsonlDecisionLedger,
                    "verified_snapshot",
                    forged_snapshot,
                ),
            ):
                self.assertTrue(
                    manager.release_pin(
                        kind=RetentionPinKind.DECISION,
                        owner_id="decision:decision-42",
                        delta_id=delta.delta_id,
                        canonical_event_digest=delta.canonical_event_digest,
                    )
                )

            self.assertEqual(attacker_calls, [])


if __name__ == "__main__":
    unittest.main()

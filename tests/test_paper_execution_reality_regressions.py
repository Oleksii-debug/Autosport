from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"
SUB_MS_QUOTE_AT = "2026-09-20T02:59:59.599001+00:00"


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def action(
    action_id: str,
    *,
    odds: str,
    stake: str,
    quote_observed_at: str = QUOTE_AT,
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=quote_observed_at,
        expires_at=EXPIRES_AT,
    )


def plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="paper-regression-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-regression",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def model(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "2",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "test-seeded-model",
        "seed": "fixed-seed",
        "max_quote_age_ms": 5_000,
        "min_delay_ms": 100,
        "max_delay_ms": 100,
        "rejected_bps": 0,
        "partial_bps": 0,
        "unknown_bps": 0,
        "partial_fill_bps": 3333,
        "max_slippage_bps": 137,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def empirical_record(
    action_id: str,
    *,
    source: str,
    accepted_odds: str = "2.50",
    accepted_stake: str = "10.00",
) -> PaperExecutionEvidenceRecord:
    return PaperExecutionEvidenceRecord(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        quote_id=f"quote-{action_id}",
        outcome=PaperAttemptOutcome.ACCEPTED,
        observed_at=STARTED_AT,
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source=source,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
    )


def authority_env(root: Path):
    return patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(root)},
        clear=False,
    )


class PaperExecutionRepairRegressions(unittest.TestCase):
    def test_ambient_decimal_context_cannot_change_durable_economics(self):
        def capture(precision: int):
            with tempfile.TemporaryDirectory() as tmp, localcontext() as ctx:
                root = Path(tmp)
                workspace = root / "workspace"
                workspace.mkdir()
                with authority_env(root / "authority"):
                    ctx.prec = precision
                    ledger = PaperExecutionLedger(workspace / "paper.jsonl")
                    result = execute_paper_plan(
                        plan=plan(
                            action("a1", odds="2.123456789", stake="99999999.99"),
                            action("a2", odds="3.987654321", stake="99999999.99"),
                        ),
                        trigger_id="trigger-decimal-invariance",
                        config=model(),
                        ledger=ledger,
                        started_at=STARTED_AT,
                    )
                    return (
                        tuple(item.to_dict() for item in result.attempts),
                        result.worst_case_exposure,
                        ledger.events(),
                    )

        low = capture(10)
        high = capture(28)
        self.assertEqual(low, high)
        self.assertEqual(low[1], Decimal("199999999.98"))

    def test_false_completion_is_rejected_and_semantic_tamper_fails_restart(self):
        current = plan(action("a1", odds="2.50", stake="10.00"))
        cfg = model(max_slippage_bps=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with authority_env(root / "authority"):
                manual = PaperExecutionLedger(workspace / "manual.jsonl")
                manual.reserve_run(
                    run_id="manual-run",
                    trigger_id="manual-trigger",
                    plan=current,
                    config=cfg,
                    started_at=STARTED_AT,
                    observation_evidence_ids={},
                )
                with self.assertRaisesRegex(
                    PaperExecutionStateError,
                    "cannot complete before",
                ):
                    manual.complete_run(
                        run_id="manual-run",
                        pending_action_ids=(),
                        recovery_decision=RecoveryDecision.NONE,
                        worst_case_exposure=Decimal("0"),
                    )

                path = workspace / "paper.jsonl"
                ledger = PaperExecutionLedger(path)
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-forged-completion",
                    config=cfg,
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

                lines = path.read_text(encoding="utf-8").splitlines()
                completion = json.loads(lines[-1])
                self.assertEqual(completion["event_type"], "RUN_COMPLETED")
                completion["payload"]["worst_case_exposure"] = "0"
                body = dict(completion)
                body.pop("event_sha256")
                completion["event_sha256"] = digest(body)
                lines[-1] = canonical(completion)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")

                anchor_path = path.with_name(path.name + ".anchor.json")
                anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
                anchor["ledger_root_sha256"] = completion["event_sha256"]
                anchor_body = dict(anchor)
                anchor_body.pop("anchor_sha256")
                anchor["anchor_sha256"] = digest(anchor_body)
                anchor_path.write_text(canonical(anchor) + "\n", encoding="utf-8")

                # Compromise the separate authority too so this regression still
                # reaches the mechanically-derived economics validation layer.
                witness_path = Path(ledger._monotonic_witness_path)
                self.assertNotEqual(witness_path.parent, path.parent)
                witness_lines = witness_path.read_text(encoding="utf-8").splitlines()
                witness = json.loads(witness_lines[-1])
                witness["ledger_root_sha256"] = completion["event_sha256"]
                witness_body = dict(witness)
                witness_body.pop("witness_sha256")
                witness["witness_sha256"] = digest(witness_body)
                witness_lines[-1] = canonical(witness)
                witness_path.write_text(
                    "\n".join(witness_lines) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "completion payload does not match durable attempt economics",
                ):
                    execute_paper_plan(
                        plan=current,
                        trigger_id="trigger-forged-completion",
                        config=cfg,
                        ledger=PaperExecutionLedger(path),
                        started_at=STARTED_AT,
                    )

    def test_restoring_every_workspace_file_cannot_roll_back_external_authority(self):
        current = plan(action("a1", odds="2.50", stake="10.00"))
        cfg = model(max_slippage_bps=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with authority_env(root / "authority"):
                path = workspace / "paper.jsonl"
                ledger = PaperExecutionLedger(path)
                registry = PaperExecutionEvidenceRegistry(ledger)
                first = empirical_record("a1", source="empirical-first")
                registry.register(first)
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-monotonic-rollback",
                    config=cfg,
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={"a1": first.as_observation()},
                    evidence_registry=registry,
                )

                witness_path = Path(ledger._monotonic_witness_path)
                self.assertTrue(witness_path.exists())
                self.assertNotEqual(witness_path.parent, workspace)
                generation_one = {
                    item.name: item.read_bytes()
                    for item in workspace.iterdir()
                    if item.is_file()
                }

                changed = empirical_record(
                    "a1",
                    source="empirical-changed",
                    accepted_odds="2.40",
                )
                registry.register(changed)

                # Restore the complete rollbackable workspace snapshot, not just
                # ledger+anchor. The independent authority remains at G2.
                for item in workspace.iterdir():
                    if item.is_file():
                        item.unlink()
                for name, payload in generation_one.items():
                    (workspace / name).write_bytes(payload)

                reopened = PaperExecutionLedger(path)
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "older than monotonic witness authority",
                ):
                    reopened.events()

                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "older than monotonic witness authority",
                ):
                    PaperExecutionEvidenceRegistry(reopened).register(changed)

    def test_deleting_all_workspace_state_after_history_fails_closed(self):
        current = plan(action("a1", odds="2.50", stake="10.00"))
        cfg = model(max_slippage_bps=0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with authority_env(root / "authority"):
                path = workspace / "paper.jsonl"
                ledger = PaperExecutionLedger(path)
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-delete-pair",
                    config=cfg,
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
                witness_path = Path(ledger._monotonic_witness_path)
                self.assertTrue(witness_path.exists())
                self.assertNotEqual(witness_path.parent, workspace)
                for item in workspace.iterdir():
                    if item.is_file():
                        item.unlink()

                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "older than monotonic witness authority",
                ):
                    PaperExecutionLedger(path).events()

    def test_sub_millisecond_synthetic_quote_age_is_rejected_conservatively(self):
        current = plan(
            action(
                "a1",
                odds="2.50",
                stake="10.00",
                quote_observed_at=SUB_MS_QUOTE_AT,
            )
        )
        cfg = model(
            max_quote_age_ms=500,
            min_delay_ms=0,
            max_delay_ms=0,
            max_slippage_bps=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with authority_env(root / "authority"):
                result = execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-sub-ms-synthetic",
                    config=cfg,
                    ledger=PaperExecutionLedger(workspace / "paper.jsonl"),
                    started_at=STARTED_AT,
                )

        self.assertEqual(result.attempts[0].quote_age_ms, 501)
        self.assertEqual(result.attempts[0].outcome, PaperAttemptOutcome.REJECTED)
        self.assertIn("freshness bound", result.attempts[0].reason)

    def test_sub_millisecond_registered_observation_is_rejected_conservatively(self):
        current = plan(
            action(
                "a1",
                odds="2.50",
                stake="10.00",
                quote_observed_at=SUB_MS_QUOTE_AT,
            )
        )
        cfg = model(
            max_quote_age_ms=500,
            min_delay_ms=0,
            max_delay_ms=0,
            max_slippage_bps=0,
        )
        observed = empirical_record("a1", source="empirical-sub-ms")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            with authority_env(root / "authority"):
                ledger = PaperExecutionLedger(workspace / "paper.jsonl")
                registry = PaperExecutionEvidenceRegistry(ledger)
                registry.register(observed)
                with self.assertRaisesRegex(
                    PaperExecutionStateError,
                    "violates configured quote freshness",
                ):
                    execute_paper_plan(
                        plan=current,
                        trigger_id="trigger-sub-ms-observed",
                        config=cfg,
                        ledger=ledger,
                        started_at=STARTED_AT,
                        observations={"a1": observed.as_observation()},
                        evidence_registry=registry,
                    )


if __name__ == "__main__":
    unittest.main()

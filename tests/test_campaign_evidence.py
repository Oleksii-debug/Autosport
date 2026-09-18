from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal, getcontext
from pathlib import Path

from autosport.campaign_evidence import (
    CampaignFinalizedError,
    CampaignIntegrityError,
    CampaignOutcome,
    CampaignReadiness,
    PaperCampaign,
    SessionEvidence,
)
from autosport.run_registry import RunRegistry
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    Hypothesis,
    ModelVersion,
    ResearchProtocol,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding


class PaperCampaignTests(unittest.TestCase):
    BASE = {
        "campaign_id": "campaign-1",
        "campaign_version": 1,
        "research_protocol_id": "protocol-1",
        "protocol_sha256": "11" * 32,
        "hypothesis_id": "hypothesis-1",
        "primary_metric": "net_profit",
        "protective_metrics": ("max_drawdown", "risk_of_ruin"),
        "evaluation_window_start": "2026-09-01T00:00:00Z",
        "evaluation_window_end": "2026-09-10T23:59:59Z",
        "evaluation_as_of": "2026-09-11T00:00:00Z",
        "readiness_rule": "predeclared campaign gate v1",
        "source_sha256": "22" * 32,
        "created_at": "2026-09-11T01:00:00Z",
        "strategy_version_id": "strategy-1",
        "model_version_id": "model-1",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.scientific_registry = ScientificRegistry.initialize_pristine(
            self.workspace / "scientific-registry.json"
        )
        self.run_registry = RunRegistry.initialize_pristine(
            self.workspace / "run-registry.json"
        )
        self._run_authorities: dict[str, tuple[str, str]] = {}

        binding = ScientificProtocolBinding(
            research_protocol_id="protocol-1",
            research_question_id="question-1",
            research_question_sha256="55" * 32,
            hypothesis_id="hypothesis-1",
            hypothesis_sha256="66" * 32,
            inclusion_criteria="predeclared included sessions",
            exclusion_criteria="predeclared excluded sessions",
            lawful_source_requirements="lawful durable source evidence",
            causal_cutoff="2026-09-03T23:59:59Z",
            evaluation_design="forward paper observation",
            feature_set_version="features-v1",
            uncertainty_method="predeclared deterministic summary",
            multiple_comparison_control="single frozen primary metric",
            robustness_checks=("restart",),
            random_seed_policy="fixed",
            stopping_rule="fixed window",
            promotion_rule="predeclared campaign gate v1",
            expected_artifacts=("run-summary",),
            code_config_sha256="77" * 32,
            frozen_at_utc="2026-08-02T00:00:00Z",
        )
        protocol = ResearchProtocol(
            binding=binding,
            source_sha256="22" * 32,
            environment_sha256="88" * 32,
            dataset_manifest_sha256="33" * 32,
            available_at_utc="2026-08-02T00:00:00Z",
        )
        self.protocol_sha256 = protocol.protocol_sha256
        for record in (
            Hypothesis(
                hypothesis_id="hypothesis-1",
                research_question_id="question-1",
                statement="paper campaign is evaluated under a frozen protocol",
                falsifiable_prediction="authoritative net profit is reproducible",
                failure_criteria="durable evidence does not reproduce",
                primary_metric="net_profit",
                protective_metrics=("max_drawdown", "risk_of_ruin"),
                created_at="2026-08-01T00:00:00Z",
            ),
            protocol,
            DatasetSnapshot(
                dataset_snapshot_id="dataset-1",
                manifest_sha256="33" * 32,
                source_identity="fixture-source",
                license_identity="fixture-license",
                causal_cutoff="2026-09-03T23:59:59Z",
                available_at_utc="2026-08-03T00:00:00Z",
                outcome_reveal_after="2026-09-04T00:00:00Z",
            ),
            ModelVersion(
                model_version_id="model-1",
                model_family="fixture-model",
                artifact_sha256="99" * 32,
                source_sha256="22" * 32,
                environment_sha256="88" * 32,
                dataset_snapshot_id="dataset-1",
                feature_set_id="features-1",
                research_protocol_id="protocol-1",
                seed=1,
                config_sha256="aa" * 32,
                created_at="2026-08-04T00:00:00Z",
            ),
            StrategyVersion(
                strategy_version_id="strategy-1",
                canonical_strategy_id="baseline-v1",
                source_sha256="22" * 32,
                environment_sha256="88" * 32,
                config_sha256="44" * 32,
                created_at="2026-08-04T00:00:00Z",
                model_version_id="model-1",
            ),
        ):
            self.scientific_registry.append(record)

    def session(self, **overrides) -> SessionEvidence:
        values = {
            "session_id": "session-1",
            "run_id": "run-1",
            "evidence_id": "evidence-1",
            "source_sha256": "22" * 32,
            "research_protocol_id": "protocol-1",
            "protocol_sha256": self.protocol_sha256,
            "dataset_snapshot_id": "dataset-1",
            "dataset_manifest_sha256": "33" * 32,
            "strategy_version_id": "strategy-1",
            "model_version_id": "model-1",
            "config_sha256": "44" * 32,
            "evaluation_window_start": "2026-09-01T00:00:00Z",
            "evaluation_window_end": "2026-09-03T23:59:59Z",
            "as_of": "2026-09-04T00:00:00Z",
            "available_at": "2026-09-04T00:00:00Z",
            "outcome_reveal_after": "2026-09-04T00:00:00Z",
            "observation_timestamps": (
                "2026-09-01T10:00:00Z",
                "2026-09-02T10:00:00Z",
                "2026-09-03T10:00:00Z",
            ),
            "starting_bankroll": Decimal("1000"),
            "ending_bankroll": Decimal("1060"),
            "net_profit": Decimal("60"),
            "turnover": Decimal("600"),
            "bets": 10,
            "wins": 6,
            "losses": 4,
            "voids": 0,
            "brier_sum": None,
            "log_loss_sum": None,
            "prediction_count": 0,
            "max_drawdown": None,
            "peak_exposure": None,
            "risk_of_ruin": None,
            "volatility": None,
            "outcome": CampaignOutcome.POSITIVE,
        }
        values.update(overrides)
        return SessionEvidence.build(**values)

    def campaign(self) -> PaperCampaign:
        values = dict(self.BASE)
        values["protocol_sha256"] = self.protocol_sha256
        return PaperCampaign(**values)

    def _ensure_run_authority(self, session: SessionEvidence) -> None:
        cached = self._run_authorities.get(session.run_id)
        if cached is None:
            market_sha256 = hashlib.sha256(
                f"market:{session.run_id}".encode("utf-8")
            ).hexdigest()
            results_sha256 = hashlib.sha256(
                f"results:{session.run_id}".encode("utf-8")
            ).hexdigest()
            paper_book_sha256 = hashlib.sha256(
                f"paper:{session.run_id}".encode("utf-8")
            ).hexdigest()
            decision_ledger_sha256 = hashlib.sha256(
                f"ledger:{session.run_id}".encode("utf-8")
            ).hexdigest()
            experiment_key = self.run_registry.begin(
                market_sha256,
                results_sha256,
                "baseline-v1",
                session.run_id,
                allow_repeat=True,
            )
            summary = {
                "schema_version": 2,
                "strategy_id": "baseline-v1",
                "strategy_runtime": {
                    "strategy_id": "baseline-v1",
                    "canonical_strategy_id": "baseline-v1",
                },
                "experiment_key": experiment_key,
                "market_sha256": market_sha256,
                "sealed_results_sha256": results_sha256,
                "run_id": session.run_id,
                "evaluation": {
                    "initial_bankroll": str(session.starting_bankroll),
                    "final_balance": str(session.ending_bankroll),
                    "committed_stake": "0",
                    "settled_stake": str(session.turnover),
                    "net_profit": str(session.net_profit),
                    "roi": "0",
                    "won": session.wins,
                    "lost": session.losses,
                    "void": session.voids,
                },
                "real_money_execution": False,
                "paper_book_sha256": paper_book_sha256,
                "decision_ledger_sha256": decision_ledger_sha256,
                "transaction_schema_version": 1,
                "transaction_run_id": session.run_id,
            }
            summary_text = json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            summary_bytes = summary_text.encode("utf-8")
            summary_sha256 = hashlib.sha256(summary_bytes).hexdigest()
            summary_path = self.workspace / f"run-{session.run_id}.json"
            summary_path.write_bytes(summary_bytes)
            manifest_path = (
                self.workspace
                / ".run-transactions"
                / session.run_id
                / "manifest.json"
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "phase": "completed",
                        "run_id": session.run_id,
                        "experiment_key": experiment_key,
                        "market_sha256": market_sha256,
                        "sealed_results_sha256": results_sha256,
                        "strategy_id": "baseline-v1",
                        "targets": {"summary": summary_path.name},
                        "new": {
                            "paper_book_sha256": paper_book_sha256,
                            "decision_ledger_sha256": decision_ledger_sha256,
                            "summary_sha256": summary_sha256,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.run_registry.complete(
                experiment_key,
                str(summary_path),
                paper_book_sha256=paper_book_sha256,
                decision_ledger_sha256=decision_ledger_sha256,
            )
            cached = (experiment_key, summary_sha256)
            self._run_authorities[session.run_id] = cached

        _, summary_sha256 = cached
        self.scientific_registry.append(
            EvaluationBundleRef(
                evaluation_bundle_id=session.evidence_id,
                bundle_sha256=hashlib.sha256(
                    f"bundle:{session.evidence_id}".encode("utf-8")
                ).hexdigest(),
                evaluator_source_sha256="bb" * 32,
                dataset_snapshot_id="dataset-1",
                protocol_sha256=self.protocol_sha256,
                artifact_hashes=(summary_sha256,),
                created_at=session.available_at,
                evaluated_strategy_version_id=session.strategy_version_id,
                evaluated_model_version_id=session.model_version_id,
            )
        )

    def add_session(
        self,
        campaign: PaperCampaign,
        session: SessionEvidence,
        *,
        establish_authority: bool = True,
    ) -> None:
        if establish_authority:
            self._ensure_run_authority(session)
        campaign.add_session(
            session,
            scientific_registry=self.scientific_registry,
            run_registry=self.run_registry,
        )

    def test_session_evidence_hash_binds_window_and_metrics(self):
        session = self.session()
        self.assertEqual(session.evidence_sha256, session.computed_evidence_sha256)
        with self.assertRaises(CampaignFinalizedError):
            campaign = self.campaign()
            self.add_session(campaign, session)
            campaign.finalize(
                outcome=CampaignOutcome.POSITIVE,
                readiness=CampaignReadiness.ELIGIBLE,
                finalized_at="2026-09-11T00:00:00Z",
            )
            self.add_session(campaign, self.session(session_id="session-2"))

    def test_model_identity_build_and_restart_roundtrip(self):
        session = self.session(model_version_id="model-1")
        self.assertEqual(session.model_version_id, "model-1")
        campaign = self.campaign()
        self.add_session(campaign, session)
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            loaded = PaperCampaign.load(
                path,
                scientific_registry=self.scientific_registry,
                run_registry=self.run_registry,
            )
        self.assertEqual(loaded.sessions[0].model_version_id, "model-1")
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)

    def test_direct_constructor_finalization_requires_canonical_authority(self):
        session = self.session()
        campaign = self.campaign()
        campaign.sessions = [session]
        with self.assertRaises(CampaignIntegrityError):
            campaign.finalize(
                outcome=CampaignOutcome.POSITIVE,
                readiness=CampaignReadiness.ELIGIBLE,
                finalized_at="2026-09-11T00:00:00Z",
            )
        self.assertFalse(campaign.finalized)
        with self.assertRaises(CampaignFinalizedError):
            campaign.finalized = True

    def test_direct_finalized_constructor_cannot_mint_authority(self):
        session = self.session()
        provisional = self.campaign()
        provisional.sessions = [session]
        provisional.finalized_at = "2026-09-11T00:00:00Z"
        provisional.outcome = CampaignOutcome.POSITIVE
        provisional.readiness = CampaignReadiness.ELIGIBLE
        digest = provisional._computed_campaign_sha256()

        values = dict(self.BASE)
        values["protocol_sha256"] = self.protocol_sha256
        with self.assertRaises(CampaignIntegrityError):
            PaperCampaign(
                **values,
                sessions=[session],
                finalized=True,
                finalized_at="2026-09-11T00:00:00Z",
                outcome=CampaignOutcome.POSITIVE,
                readiness=CampaignReadiness.ELIGIBLE,
                campaign_sha256=digest,
            )

    def test_finalized_load_requires_canonical_authority(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            with self.assertRaises(CampaignIntegrityError):
                PaperCampaign.load(path)
            loaded = PaperCampaign.load(
                path,
                scientific_registry=self.scientific_registry,
                run_registry=self.run_registry,
            )
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        self.assertTrue(loaded.finalized)

    def test_available_after_as_of_is_rejected(self):
        with self.assertRaises(ValueError):
            self.session(
                as_of="2026-09-12T00:00:00Z",
                available_at="2026-09-13T00:00:00Z",
            )

    def test_campaign_rejects_evidence_after_evaluation_as_of(self):
        campaign = self.campaign()
        session = self.session(
            as_of="2026-09-12T00:00:00Z",
            available_at="2026-09-12T00:00:00Z",
        )
        with self.assertRaises(ValueError):
            self.add_session(campaign, session)

    def test_sample_membership_is_mechanically_checked(self):
        with self.assertRaises(ValueError):
            self.session(observation_timestamps=("2026-08-31T23:59:59Z",) * 1)

    def test_mismatched_strategy_is_rejected_by_campaign(self):
        campaign = self.campaign()
        with self.assertRaises(ValueError):
            self.add_session(
                campaign,
                self.session(strategy_version_id="strategy-2"),
            )

    def test_duplicate_session_membership_is_rejected(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        with self.assertRaises(ValueError):
            self.add_session(campaign, self.session(session_id="session-1", run_id="run-2", evidence_id="evidence-2"))

    def test_negative_and_null_outcomes_are_preserved(self):
        campaign = self.campaign()
        self.add_session(
            campaign,
            self.session(
                ending_bankroll=Decimal("990"),
                net_profit=Decimal("-10"),
                outcome=CampaignOutcome.NEGATIVE,
            ),
        )
        self.add_session(
            campaign,
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                ending_bankroll=Decimal("1000"),
                net_profit=Decimal("0"),
                outcome=CampaignOutcome.NULL,
            ),
        )
        summary = campaign.finalize(
            outcome=CampaignOutcome.INCONCLUSIVE,
            readiness=CampaignReadiness.INCONCLUSIVE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        self.assertEqual(summary.outcome, CampaignOutcome.INCONCLUSIVE)
        self.assertEqual(campaign.sessions[0].outcome, CampaignOutcome.NEGATIVE)
        self.assertEqual(campaign.sessions[1].outcome, CampaignOutcome.NULL)

    def test_aggregate_metrics_are_deterministic_across_decimal_contexts(self):
        campaign_a = self.campaign()
        self.add_session(campaign_a, self.session())
        self.add_session(campaign_a, 
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                starting_bankroll=Decimal("2000"),
                ending_bankroll=Decimal("2110"),
                net_profit=Decimal("110"),
                turnover=Decimal("1000"),
                bets=20,
                wins=11,
                losses=9,
            )
        )
        original_context = getcontext().copy()
        getcontext().prec = 6
        first = campaign_a.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        getcontext().clear_flags()
        getcontext().prec = original_context.prec
        getcontext().rounding = original_context.rounding
        getcontext().Emin = original_context.Emin
        getcontext().Emax = original_context.Emax
        getcontext().capitals = original_context.capitals
        getcontext().clamp = original_context.clamp
        getcontext().traps = original_context.traps.copy()
        campaign_b = self.campaign()
        self.add_session(campaign_b, self.session())
        self.add_session(campaign_b, 
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                starting_bankroll=Decimal("2000"),
                ending_bankroll=Decimal("2110"),
                net_profit=Decimal("110"),
                turnover=Decimal("1000"),
                bets=20,
                wins=11,
                losses=9,
            )
        )
        second = campaign_b.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        self.assertEqual(campaign_a.campaign_sha256, campaign_b.campaign_sha256)
        self.assertEqual(first.campaign_sha256, second.campaign_sha256)

    def test_finalize_freezes_membership_and_fork_creates_new_version(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        campaign.finalize(
            outcome=CampaignOutcome.HARMFUL,
            readiness=CampaignReadiness.NOT_ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with self.assertRaises(CampaignFinalizedError):
            campaign.remove_session("session-1")
        fork = campaign.fork_new_version(2)
        self.assertFalse(fork.finalized)
        self.assertEqual(fork.sessions, [])

    def test_finalized_campaign_seals_membership_and_public_state(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        frozen = campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        replacement = self.session(
            session_id="session-2",
            run_id="run-2",
            evidence_id="evidence-2",
        )

        self.assertIsInstance(campaign.sessions, tuple)
        with self.assertRaises(AttributeError):
            campaign.sessions.append(replacement)
        with self.assertRaises(CampaignFinalizedError):
            campaign.sessions = [replacement]
        with self.assertRaises(CampaignFinalizedError):
            campaign.readiness = CampaignReadiness.NOT_ELIGIBLE
        with self.assertRaises(CampaignFinalizedError):
            campaign.finalized = False

        self.assertEqual(campaign.summary(), frozen)

    def test_draft_cannot_self_assert_finalized_state(self):
        campaign = self.campaign()
        with self.assertRaises(CampaignFinalizedError):
            campaign.finalized = True

    def test_restart_roundtrip_preserves_final_identity_and_summary(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            loaded = PaperCampaign.load(
                path,
                scientific_registry=self.scientific_registry,
                run_registry=self.run_registry,
            )
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        self.assertEqual(loaded.export_summary(), campaign.export_summary())
        self.assertTrue(loaded.finalized)
        with self.assertRaises(CampaignFinalizedError):
            loaded.readiness = CampaignReadiness.NOT_ELIGIBLE

    def test_tampered_state_is_rejected(self):
        campaign = self.campaign()
        self.add_session(campaign, self.session())
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["sessions"][0]["net_profit"] = "61"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(CampaignIntegrityError):
                PaperCampaign.load(path)

    def test_authoritative_admission_rejects_mutated_dataset_manifest(self):
        authoritative = self.session()
        self._ensure_run_authority(authoritative)
        mutated = self.session(dataset_manifest_sha256="cc" * 32)
        with self.assertRaises(CampaignIntegrityError):
            self.add_session(
                self.campaign(),
                mutated,
                establish_authority=False,
            )

    def test_authoritative_admission_rejects_mutated_economic_metrics(self):
        authoritative = self.session()
        self._ensure_run_authority(authoritative)
        mutated = self.session(
            ending_bankroll=Decimal("1061"),
            net_profit=Decimal("61"),
        )
        with self.assertRaises(CampaignIntegrityError):
            self.add_session(
                self.campaign(),
                mutated,
                establish_authority=False,
            )

    def test_authoritative_admission_rejects_summary_tamper(self):
        session = self.session()
        self._ensure_run_authority(session)
        path = self.workspace / f"run-{session.run_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["evaluation"]["net_profit"] = "999"
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(CampaignIntegrityError):
            self.add_session(
                self.campaign(),
                session,
                establish_authority=False,
            )

    def test_unbacked_optional_metrics_are_rejected_instead_of_minted(self):
        authoritative = self.session()
        self._ensure_run_authority(authoritative)
        mutated = self.session(
            brier_sum=Decimal("0.2"),
            prediction_count=10,
        )
        with self.assertRaises(CampaignIntegrityError):
            self.add_session(
                self.campaign(),
                mutated,
                establish_authority=False,
            )

    def test_export_summary_is_explicit_about_unsupported_metrics(self):
        campaign = self.campaign()
        self.add_session(campaign, 
            self.session(
                brier_sum=None,
                log_loss_sum=None,
                prediction_count=0,
                max_drawdown=None,
                peak_exposure=None,
                risk_of_ruin=None,
                volatility=None,
            )
        )
        summary = campaign.export_summary()
        self.assertIsNone(summary["metrics"]["brier_score"])
        self.assertIsNone(summary["metrics"]["risk_of_ruin"])
        self.assertIn("brier_score=TBD", campaign.evidence_summary())


if __name__ == "__main__":
    unittest.main()

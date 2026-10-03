from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

import autosport.risk_membership_publication as publication
import autosport.risk_randomization_precommit as randomization
from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
from autosport.continuous_session import SettlementResolution
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper import PaperBook
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.risk import PaperRiskPolicy
from autosport.risk_path_observation_authority import (
    ProductRunCapitalPathError,
    _conservative_minimum_equity,
    resolve_product_run_capital_path_evidence,
)
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction


RUN_ID = "run-001"


def _membership() -> ResolvedFixedNRiskMembership:
    return ResolvedFixedNRiskMembership(
        research_protocol_id="risk-fixed-n-protocol",
        protocol_sha256="2" * 64,
        protocol_record_sha256="9" * 64,
        dataset_snapshot_id="risk-fixed-n-dataset",
        dataset_manifest_sha256="3" * 64,
        dataset_record_sha256="a" * 64,
        causal_cutoff="2026-09-01T00:00:00+00:00",
        outcome_reveal_after="2026-09-10T00:00:00+00:00",
        precommitted_at="2026-09-02T12:05:00+00:00",
        planned_run_ids=(RUN_ID,),
        sampling_frame_sha256="4" * 64,
        design_sha256="1" * 64,
    )


def _sampling_manifest(*, randomization_root_sha256: str) -> str:
    payload = {
        "kind": "autosport-risk-iid-resample-with-replacement-v1",
        "experiment_id": "iid-risk-exp-001",
        "membership_design_sha256": "1" * 64,
        "membership_protocol_record_sha256": "9" * 64,
        "membership_dataset_record_sha256": "a" * 64,
        "membership_causal_cutoff": "2026-09-01T00:00:00+00:00",
        "membership_precommitted_at": "2026-09-02T12:05:00+00:00",
        "membership_outcome_reveal_after": "2026-09-10T00:00:00+00:00",
        "research_protocol_id": "risk-fixed-n-protocol",
        "protocol_sha256": "2" * 64,
        "dataset_snapshot_id": "risk-fixed-n-dataset",
        "dataset_manifest_sha256": "3" * 64,
        "sampling_frame_sha256": "4" * 64,
        "initial_capital_state_sha256": "5" * 64,
        "stake_policy_sha256": "6" * 64,
        "horizon_sha256": "7" * 64,
        "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
        "with_replacement": True,
        "rng_algorithm": "PCG64",
        "rng_version": "numpy-compatible-contract-v1",
        "randomization_root_sha256": randomization_root_sha256,
        "planned_n": 1,
        "planned_member_ids": [RUN_ID],
        "stopping_rule": "FIXED_N_NO_EARLY_STOP",
        "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
        "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _product_precommit(tmp_path, monkeypatch):
    membership = _membership()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_path = workspace / "scientific-registry.json"
    registry_path.write_text("{}\n", encoding="utf-8")
    RunRegistry.initialize_pristine(workspace / "run_registry.json")
    authority_root = tmp_path / "authority"

    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: membership,
    )
    publication.publish_fixed_n_membership_structure(
        registry_path,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        authority_root=authority_root,
    )
    issued = randomization.issue_risk_randomization_precommit(
        registry_path,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="iid-risk-exp-001",
        authority_root=authority_root,
    )
    manifest = _sampling_manifest(
        randomization_root_sha256=issued.randomization_root_sha256,
    )
    return membership, workspace, registry_path, authority_root, manifest


def _completed_run_with_settlement_bridge(
    workspace,
):
    goal = EconomicGoalContract(
        goal_id="risk-path-goal",
        revision=1,
        bankroll_id="risk-path-bankroll",
        currency="USD",
    )
    risk = PaperRiskPolicy(economic_goal=goal)
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2.00"),
        sport="table_tennis",
    )
    book_path = workspace / "paper_book.json"
    book.save(book_path)
    base_book_sha = hashlib.sha256(book_path.read_bytes()).hexdigest()

    canonical_ledger = workspace / "decisions.jsonl"
    canonical_ledger.write_bytes(b"")
    base_ledger_sha = hashlib.sha256(b"").hexdigest()

    registry = RunRegistry(workspace / "run_registry.json")
    experiment_key = registry.begin(
        "b" * 64,
        "c" * 64,
        "risk-path-strategy",
        RUN_ID,
        base_paper_book_sha256=base_book_sha,
        base_decision_ledger_sha256=base_ledger_sha,
    )
    tx = RunTransaction.start(
        workspace,
        run_id=RUN_ID,
        experiment_key=experiment_key,
        market_sha256="b" * 64,
        results_sha256="c" * 64,
        strategy_id="risk-path-strategy",
        base_paper_book_sha256=base_book_sha,
        base_decision_ledger_sha256=base_ledger_sha,
    )
    book = PaperBook.load(book_path)
    ticket = book.open_ticket(
        (leg,),
        Decimal("10"),
        placed_at="2026-09-03T10:00:10+00:00",
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
    )

    identity = EnvironmentIdentity(
        source_id="risk-path-source",
        config_id="risk-path-config",
        data_id="risk-path-data",
        protocol_id="risk-path-protocol",
        cutoff_ts="2026-09-03T10:00:00+00:00",
        seed=7,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="risk-path-episode",
        policy_id="risk-path-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-03T10:00:00+00:00",
        available_at="2026-09-03T10:00:01+00:00",
        evidence=(("market_state", "risk-path-snapshot"),),
    )
    run_ledger = JsonlDecisionLedger(tx.run_ledger_path)
    decision = DecisionRecord(
        replay_run_id=RUN_ID,
        agent="risk-path-fixture",
        observed_ts=observation.observed_at,
        action="OPEN_PAPER_TICKET",
        payload={
            "ticket_id": ticket.ticket_id,
            "stake": str(ticket.stake),
            "quote_key": leg.quote_key,
        },
        context_hash=observation.observation_id,
        decision_id="risk-path-decision",
        decision_kind=ECONOMIC_DECISION_KIND,
    )
    run_ledger.append_economic(
        decision,
        EconomicDecisionAuthority(goal, risk),
    )

    baseline = environment.checkpoint()
    runtime = AgentLoopRuntime.initialize_pristine(
        workspace / "agent-loop.json",
        loop_id="risk-path-loop",
        environment_checkpoint=baseline,
        policy_id=environment.episode.policy_id,
        economic_goal_fingerprint=provenance_for(goal).contract_sha256,
        risk_fingerprint=risk.provenance_sha256,
        source_sha256="d" * 64,
        config_sha256="e" * 64,
        at="2026-09-03T09:59:59+00:00",
    )
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-03T10:00:01+00:00",
    )
    for phase in (
        AgentLoopPhase.OBSERVE,
        AgentLoopPhase.ASSESS,
        AgentLoopPhase.PLAN,
        AgentLoopPhase.DECIDE,
    ):
        runtime.advance(
            expected=phase,
            at="2026-09-03T10:00:02+00:00",
        )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-03T10:00:05+00:00",
        parameters=(
            ("economic_decision_id", decision.decision_id),
            ("paper_ticket_id", ticket.ticket_id),
        ),
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at="2026-09-03T10:00:05+00:00",
    )

    bridge = PaperSettlementLearningBridge(
        workspace / "paper_learning_bridge.json",
        paper_book_path=book_path,
        decision_ledger=run_ledger,
        agent_loop=runtime,
        economic_goal=goal,
        risk_policy=risk,
    )
    bridge.bind_ticket(
        ticket_id=ticket.ticket_id,
        decision_id=decision.decision_id,
        environment=environment,
        observation=observation,
        action=action,
        baseline_checkpoint=baseline,
    )

    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="settlement-evidence-1",
        evidence_sha256="f" * 64,
        available_at="2026-09-03T10:00:30+00:00",
    )
    prepared = bridge.prepare_settlement(
        paper_book_path=book_path,
        resolutions=(resolution,),
        at="2026-09-03T10:00:30+00:00",
    )
    assert prepared == (ticket.ticket_id,)

    book.settle(
        ticket.ticket_id,
        {leg.quote_key},
    )
    new_book_sha, new_ledger_sha = tx.stage_outputs(
        book,
        canonical_ledger,
    )
    tx.precommit(
        {
            "schema_version": 2,
            "run_id": RUN_ID,
            "experiment_key": experiment_key,
            "market_sha256": "b" * 64,
            "sealed_results_sha256": "c" * 64,
            "strategy_id": "risk-path-strategy",
            "real_money_execution": False,
        }
    )
    summary_path = tx.commit()
    registry.complete(
        experiment_key,
        str(summary_path),
        paper_book_sha256=new_book_sha,
        decision_ledger_sha256=new_ledger_sha,
    )
    tx.mark_registry_completed()

    acknowledged = bridge.reconcile_after_settlement(
        paper_book_path=book_path,
        resolutions=(),
        settled_ticket_ids=(ticket.ticket_id,),
        at="2026-09-03T10:00:31+00:00",
    )
    assert len(acknowledged) == 1
    return tx, bridge, ticket


def test_product_run_capital_path_re_resolves_completed_settlement(
    tmp_path,
    monkeypatch,
) -> None:
    membership, workspace, registry_path, authority_root, manifest = (
        _product_precommit(tmp_path, monkeypatch)
    )
    _tx, bridge, ticket = _completed_run_with_settlement_bridge(workspace)

    evidence = resolve_product_run_capital_path_evidence(
        workspace=workspace,
        run_id=RUN_ID,
        member_index=0,
        membership=membership,
        registry_path=registry_path,
        sampling_manifest_json=manifest,
        settlement_bridge=bridge,
        authority_root=authority_root,
    )

    assert evidence.member_id == RUN_ID
    assert evidence.member_index == 0
    assert evidence.changed_ticket_ids == (ticket.ticket_id,)
    assert evidence.minimum_equity == Decimal("90")
    assert evidence.outcome_available_at == "2026-09-03T10:00:30+00:00"
    assert evidence.product_precommit_bound is True
    assert evidence.run_path_ancestry_proven is True
    assert evidence.sampling_occurrence_ancestry_proven is False
    assert evidence.iid_qualified is False
    assert evidence.grants_real_money_authority is False


def test_run_suffix_replacement_cannot_mint_path_ancestry(
    tmp_path,
    monkeypatch,
) -> None:
    membership, workspace, registry_path, authority_root, manifest = (
        _product_precommit(tmp_path, monkeypatch)
    )
    tx, bridge, _ticket = _completed_run_with_settlement_bridge(workspace)
    tx.run_ledger_path.write_bytes(b"")

    with pytest.raises(
        ProductRunCapitalPathError,
        match="no retained run decisions",
    ):
        resolve_product_run_capital_path_evidence(
            workspace=workspace,
            run_id=RUN_ID,
            member_index=0,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=manifest,
            settlement_bridge=bridge,
            authority_root=authority_root,
        )


def test_forged_randomization_root_cannot_rebind_path_member(
    tmp_path,
    monkeypatch,
) -> None:
    membership, workspace, registry_path, authority_root, _manifest = (
        _product_precommit(tmp_path, monkeypatch)
    )
    _tx, bridge, _ticket = _completed_run_with_settlement_bridge(workspace)
    forged = _sampling_manifest(randomization_root_sha256="0" * 64)

    with pytest.raises(
        ProductRunCapitalPathError,
        match="precommit authority cannot be re-resolved",
    ):
        resolve_product_run_capital_path_evidence(
            workspace=workspace,
            run_id=RUN_ID,
            member_index=0,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=forged,
            settlement_bridge=bridge,
            authority_root=authority_root,
        )


def test_conservative_minimum_never_relies_on_settlement_order(tmp_path) -> None:
    base = PaperBook("100")
    base_path = tmp_path / "base.json"
    base.save(base_path)
    base_loaded = PaperBook.load_bytes(base_path.read_bytes())

    final = PaperBook("100")
    first = final.open_ticket(
        (
            TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="a",
                locked_odds=Decimal("2"),
                sport="soccer",
            ),
        ),
        Decimal("80"),
        placed_at="2026-09-03T10:00:00+00:00",
    )
    final.settle(first.ticket_id, {first.legs[0].quote_key})
    final.open_ticket(
        (
            TicketLeg(
                event_id="event-2",
                market_id="winner",
                selection_id="b",
                locked_odds=Decimal("2"),
                sport="soccer",
            ),
        ),
        Decimal("80"),
        placed_at="2026-09-03T10:01:00+00:00",
    )

    assert final.balance == Decimal("100")
    assert _conservative_minimum_equity(base_loaded, final) == Decimal("0")

# Cross-project continual-learning reuse — 2026-10-23

## Scope

This record documents the first repository-native integration slice for a real,
provider-free Autosport learning cycle. It changes no bookmaker, real-money,
risk-limit, champion-promotion or deployment authority.

## Reviewed first-party baselines

- `Oleksii-debug/12-6-ai.` at `8880097de205838f08f6cccd3f1e99e11fed831e`
  - bounded continual cycles;
  - separate new/replay populations;
  - frozen retention and holdout suites;
  - deterministic restart/replay verification;
  - explicit rollback and candidate-only output.
- `Oleksii-debug/AutoTrade` at `12e9b272b1a2fedacf602294d55d80b4e0315468`
  - bounded live-cycle phases;
  - protected/recent-regime retention gates;
  - no-network/no-trading-authority learning boundary;
  - prospective paired shadow evaluation.
- `Oleksii-debug/Nika-Core` at `7dedf6ad2f65c8fe80d15ec91b5a098dba833fe5`
  - durable multi-agent lifecycle and independent experiment/evaluation roles.

The implementation does not copy a foreign trainer or domain model. It adapts
the verified control ideas to Autosport's existing canonical types:
`BanditPolicyState`, `Action`, `RewardEvidence`, `Transition`,
`PolicyEvaluationCase`, `QualifiedCounterfactualAuthority` and
`evaluate_policy_pair`.

## Integrated capability

`src/autosport/continual_learning_cycle.py` now provides one bounded cycle that:

1. requires both new and replay causal experiences;
2. rejects stale champion bindings and overlapping train/evaluation populations;
3. optionally requires factual `OBSERVED` rewards;
4. applies only canonical transparent-policy updates;
5. evaluates the exact candidate against separate frozen retention and untouched
   holdout suites;
6. rejects aggregate or per-regime retention degradation;
7. checks holdout improvement and effective sample size;
8. returns either `CANDIDATE_READY` or an exact rollback to the current champion;
9. never grants promotion authority; and
10. can be deterministically recomputed after restart.

## Qualification in this slice

Focused tests execute a genuine in-memory learning update with no model API:

- positive new+replay learning changes the selected action and passes frozen
  retention/holdout gates;
- a protected-regime regression rolls back to the exact champion;
- train/holdout contamination fails before mutation; and
- a forged self-promotion receipt is rejected.

This is source/test evidence for a candidate-producing learning core, not a
claim of profitable edge, a long-running campaign, Windows operator wiring,
champion promotion, bookmaker execution or whole-product completion.

## Remaining operational convergence

The next integration slices are deliberately downstream rather than competing
authorities:

- strict file/CLI request adapter for operator-started cycles;
- canonical product-utility evidence before factory retest;
- Strategy/Model Factory candidate publication and independent promotion gate;
- Windows/WebView2 status and keyboard controls;
- prospective multi-session PAPER/SHADOW campaign using lawful sports data.

## 2026-10-24 durable AutoTrade control reuse

The next slice reviewed `AutoTrade@12e9b272b1a2fedacf602294d55d80b4e0315468`,
especially its SQLite-owned live-cycle state and research-job restart controls.
Autosport now reuses the applicable control design, not its trading domain or
models, in `src/autosport/continual_learning_journal.py`:

1. the complete canonical cycle request is durably stored before work starts;
2. an immutable request digest prevents a cycle identity from being rebound;
3. every new/replay update commits an exact policy checkpoint and witness;
4. checkpoint publication uses compare-and-swap revision fencing;
5. restart replays the committed prefix from immutable inputs before trusting
   the stored policy snapshot;
6. an append-only hash chain records queued, checkpoint and terminal phases;
7. finalization recomputes the existing canonical in-memory cycle and refuses
   any disagreement with the durable checkpoint;
8. exact redelivery is idempotent; and
9. only a qualified cycle exposes a Factory candidate envelope, with explicit
   `promotion_authorized=false` and `execution_authorized=false`.

Focused qualification covers injected process failure after the first durable
update, cross-instance resume, exact result equivalence, idempotent redelivery,
cycle identity conflicts, rollback without handoff, candidate envelope identity,
and fail-closed checkpoint/event tamper detection.

This closes the durable journal/checkpoint/crash-resume slice. It does not yet
provide a user-facing CLI, automatically publish into the Strategy/Model Factory,
or start a long-running bookmaker/data campaign.

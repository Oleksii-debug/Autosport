# Account C — canonical forward reward-drift binding, 2026-10-26

## Implemented seam

`forward_campaign_reward_drift.py` replaces no drift algorithm or persistence
authority. It adds a read-only composition proof between the existing
`DriftMonitor` / `ScientificRegistry` and the exact population/session manifests
from [PR #2581](https://github.com/Oleksii-debug/Autosport/pull/2581).

Previously, a session carried a well-formed drift SHA without proving that the
referenced finding described its actual samples. This block binds those samples
to complete canonical window witnesses, the scientific lineage, a precommitted
baseline set, and the session's exact drift digest.

The parent is pinned at `677bd939fe4dd9a62fcce7ba2f151d15e6eda9de`; its parent
[PR #2576](https://github.com/Oleksii-debug/Autosport/pull/2576) is also unmerged.
No parent candidate, Plan 6 file, durable journal, CLI, Factory, or
`continual_learning_cycle.py` was modified.

## Authority reuse

- `DriftMonitor.require_canonical_finding` re-proves persisted reference/current
  window witnesses, source DatasetSnapshots, exact arithmetic, effective sample
  policy, finding state, recommendation and evidence identity.
- `ScientificRegistry` remains the only durable store for drift records. The new
  binder does not append to it; tests compare its bytes before/after verification.
- `ForwardPopulationSample` supplies immutable sample/source/regime, causal
  timestamps, terminal state and observed exact reward.
- `ForwardEvidenceProtocolEnvelope` remains the precommit authority. Its
  `baseline_set_sha256` binds `FrozenForwardRewardDriftPlan.plan_sha256`.
- The assembly/population owners still build and validate all session chains,
  negative-evidence denominators and population-separation firewalls.

`FrozenForwardRewardDriftPlan` commits the model/strategy/feature context plus
sorted (source, regime, canonical reference ID, baseline experiment ID) tuples.
Every selected reference must have been available by the protocol's precommit
anchor lower edge. Post-outcome baseline or context selection breaks that hash.
Different regime baselines may legitimately have different experiment IDs.

## Exact diagnostic population

The only scalar inputs are observed ACTION rewards from NEW/REPLAY members,
ordered by sample ID. Both observation and availability timestamps for each
scalar are `outcome_available_at`, never the earlier observation/decision time.
The declared current window and as-of boundary equal the closed session interval.

RETENTION and HOLDOUT are excluded from values and recommendations. They undergo
the ordinary structural sample/chronology checks but their outcome labels do not
enter the diagnostic binding digest. Changing their rewards leaves that digest
unchanged in the regression test. The receipt reports only their excluded count.

WAIT/NO_BET/rejection members remain explicit training-population negative
evidence. They contribute to the per-scope negative count and exact membership
hashes, but never receive a synthetic zero reward. A negative-only scope has an
empty canonical reward window and the existing monitor correctly returns
`INSUFFICIENT_EVIDENCE`. An evaluation-only session has no training drift scopes
and exports no drift recommendation.

The existing generic `FORECAST_PERFORMANCE` kind hosts a separately identified
PAPER action-reward diagnostic, pinned by `PAPER_REWARD_DRIFT_METRIC_SHA256`.
This is not a forecast-accuracy, calibrated-probability or economic-edge claim.
Reward-unit/currency consistency remains the upstream reward authority's
responsibility; this adapter performs no currency conversion or profit inference.
Decimal formatting is exact and bounded before exponent expansion.

## Composition and restart

1. Create the canonical historical drift references with the existing monitor;
   precommit their plan hash in the forward protocol.
2. Admit actual samples with the existing source-rights/point-in-time/PAPER or
   abstention adapters. Build the current per-source/per-regime reward windows
   and findings through the existing monitor.
3. Call `bind_forward_session_reward_drift` with the exact session samples and
   one canonical finding per NEW/REPLAY source/regime scope. It compares the
   complete expected window digest, not only a count or mean.
4. Put the resulting `binding_sha256` into the existing
   `ForwardAssemblySessionWindow.drift_evidence_sha256`, then assemble the campaign
   through the existing authority.
5. `verify_forward_campaign_reward_drift` requires exactly one binding per
   session, replays all scientific authorities, and checks each session digest.
6. After restart, reopen the canonical registry and call
   `restore_forward_session_reward_drift` on saved canonical JSON plus fresh
   authority inputs. Extra fields, inflated flags, changed counts, stale registry
   records or any changed binding fail comparison. The saved JSON is not itself
   positive scientific authority.

All operations in this new module are read-only. The durable campaign journal
and orchestration remain owned by their existing account; no competing scheduler
or collect/train/settle loop is introduced.

## Qualification and evidence class

Reproduction commands:

```text
PYTHONPATH=src python -m pytest -q tests/test_forward_campaign_reward_drift.py
19 passed in 138.02s
```

Static checks:

```text
git diff --check
python -m py_compile src/autosport/forward_campaign_reward_drift.py \
  tests/test_forward_campaign_reward_drift.py
```

Cross-authority regression:

```text
PYTHONPATH=src python -m pytest -q \
  tests/test_forward_campaign_assembly.py \
  tests/test_forward_campaign_authority_adapter.py \
  tests/test_forward_campaign_population.py \
  tests/test_forward_learning_population_binding.py \
  tests/test_continual_learning_cycle.py \
  tests/test_policy_evaluation.py \
  tests/test_forward_evidence_completeness.py \
  tests/test_forward_evidence_completeness_blocking_falsifiers.py \
  tests/test_forward_evidence_anchor_causal_order.py \
  tests/test_forward_evidence_precommit_all_observations.py \
  tests/test_paper_settlement_learning.py \
  tests/test_paper_abstention_learning.py \
  tests/test_point_in_time_evidence.py \
  tests/test_source_rights_manifest.py \
  tests/test_drift_control.py \
  tests/test_forward_campaign_reward_drift.py
259 passed in 205.96s
```

Tests exercise NO_DRIFT, DRIFT_DETECTED and INSUFFICIENT_EVIDENCE through the
actual canonical monitor, plus fully canonical but wrong reward/availability/
holdout/negative-zero windows; missing/duplicate/foreign scopes; late baseline;
wrong metric; post-precommit context mutation; crosswired baseline regimes;
future finding availability; fabricated session hashes; tampered receipt JSON;
evaluation-label independence; registry reopen/replay; and 10-session / 40-sample
synthetic endurance.

Recorded two-session fixture readback:

```text
metric_definition_sha256=64e5c94a24caf67109c20c96acb808cd6f18b4cb7699c08215289ba215f00873
plan_sha256=4c0a6453c8717356a59732e2353b2981ec9ef641ebd3636882127de07e704e22
session-1.binding_sha256=3038c93dbbfa1b887856056f0cb8ef77c8685f7de35b69e3a75fb8182a6b3ef3
session-2.binding_sha256=75d4f28e1b064e78339d008f434ea3e6c253ceda66b3ca0fee9c22356f354e26
```

Both sessions exclude 2 evaluation members and preserve 1 negative-only scope
with INSUFFICIENT_EVIDENCE. The eligible ACTION scope is NO_DRIFT in session 1
and DRIFT_DETECTED in session 2. Those are synthetic diagnostic states, not
claims about sporting markets.

These are source/fixture/engineering tests, not evidence that real sporting data
was acquired or a real long-running forward campaign completed. Source rights,
external inventories and anchor authenticity remain upstream evidence gates.

## Integration status and scientific boundary

At live readback, main remains
`70f7f5ff6530da9069bc6dfe2a28e62e0eb2bec8`; #2576 and #2581 are unmerged. Plan 6
convergence [#2573](https://github.com/Oleksii-debug/Autosport/pull/2573) at
`fda71963aa3900c9ad17dd56b4eeac3ba3c4de35` has successful CI but a failed Windows
candidate. Independent UIA diagnostic successes do not substitute for that
failed product gate. No Plan 6 repair or merge is performed by this block.

`profitability_claim=false`, `promotion_authorized=false`, and
`real_money_execution=false` are literal receipt fields. Even DRIFT_DETECTED
permits only the canonical bounded research recommendation; no automatic
training, promotion, Risk/owner change or execution is authorized here. Untouched
real forward evidence, real-money, physical NVDA and whole-product completion
remain unclaimed.

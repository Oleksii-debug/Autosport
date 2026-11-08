# Account C — forward population firewall evidence (2026-10-24)

## Scope and reuse decision

This block adds no collector, replay engine, settlement authority, evaluator,
scientific registry, durable campaign journal/CLI, Strategy/Model Factory, or
Windows/Plan-6 implementation.

The implementation reuses these existing canonical authorities:

- `product_source.py` plus `source_rights_manifest.py` for human-approved,
  read-only source-rights authorization and timestamped acquisition;
- `causal_collector.py`, `replay.py`, `market_outcomes.py`, and
  `paper_settlement_learning.py` for causal observations, replay, outcome
  availability, and PAPER settlement-to-learning evidence;
- `forward_evidence_completeness.py` for prospective denominator/negative
  evidence truth;
- `policy_evaluation.py`, `campaign_evidence.py`, `scientific_registry.py`, and
  `drift_control.py` for evaluation, campaign, scientific, and drift authority;
- `continual_learning_cycle.py` from merged PR #2571 for candidate-only
  new/replay learning with frozen retention and holdout evaluation.

Open draft PR #1426 owns a separate forward-economic proof surface and is not
modified or duplicated by this block.

## Added contract

`src/autosport/forward_campaign_population.py` is an immutable admission and
evidence manifest. It requires:

1. independent non-empty `NEW`, `REPLAY`, `RETENTION`, and `HOLDOUT`
   populations;
2. no duplicate or cross-population sample identity, event identity, or source
   evidence digest;
3. `observed <= available <= immutable decision cutoff <= decision`;
4. outcome evidence becoming available strictly after the decision and
   settlement not preceding outcome availability;
5. every non-action result retained as explicit negative evidence without an
   invented reward;
6. exact per-session/per-regime counts and observed reward sums;
7. a canonical drift-evidence digest for every session;
8. a gap-free, non-overlapping, hash-chained multi-session sequence;
9. exact JSON restart round-trip and campaign-manifest digest verification; and
10. literal `profitability_claim=false`, `promotion_authorized=false`, and
    `real_money_execution=false`.

The contract does not interpret an observed reward sum as profitability or edge.
Untouched forward evidence and the existing independent scientific/promotion
authorities remain mandatory.

## Reproducible qualification

Focused firewall suite:

```text
python -m pytest -q tests/test_forward_campaign_population.py
16 passed in 0.67s
```

Cross-authority regression:

```text
python -m pytest -q \
  tests/test_forward_campaign_population.py \
  tests/test_forward_evidence_completeness.py \
  tests/test_forward_evidence_completeness_blocking_falsifiers.py \
  tests/test_forward_policy_comparison.py \
  tests/test_drift_control.py \
  tests/test_policy_evaluation.py \
  tests/test_continual_learning_cycle.py \
  tests/test_paper_settlement_learning.py \
  tests/test_campaign_evidence.py
169 passed in 107.06s
```

The focused suite includes a deterministic 40-session, 800-sample endurance and
restart round-trip. It uses synthetic evidence identities to qualify the
contract; it is not a claim that an 800-sample real forward campaign occurred.

## Truth boundary

- profitable edge: **NOT CLAIMED**;
- untouched real forward outcome: **NOT YET ACCUMULATED BY THIS CODE BLOCK**;
- champion promotion: **NOT AUTHORIZED**;
- real-money execution: **FALSE**;
- physical NVDA acceptance: **NOT PART OF THIS BLOCK**.


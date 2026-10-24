# Account C forward-campaign authority adapter — 2026-10-24

## Outcome

`forward_campaign_authority_adapter.py` closes the typed seam from Autosport's
existing lawful source, point-in-time, forward-campaign, PAPER settlement, and
abstention authorities into the immutable forward population firewall. It does
not collect data, derive settlement truth, persist a journal, train or evaluate a
policy, promote a model, or authorize execution.

## Reused authorities

- `source_rights_manifest.py` owns human-approved source identity, scope, terms,
  retention authority, effective dates, and the immutable rights-manifest hash.
- `point_in_time_evidence.py` owns feature/source revision provenance and the
  observed/available/decision-cutoff chronology.
- `forward_evidence_completeness.py` owns precommitted opportunity order,
  denominator completeness, authoritative source receipts, cohort roots, campaign
  close, and reveal-boundary evidence.
- `paper_settlement_learning.py` owns observed PAPER action/outcome/reward and
  checkpoint-linked settlement witnesses.
- `paper_abstention_learning.py` owns finalized WAIT/NO_BET terminal receipts.
- `forward_campaign_population.py` owns population samples and downstream
  sample/event/source-evidence separation.
- `continual_learning_cycle.py` owns `VerifiedLearningExperience`; it is consumed
  as a type only and was not modified.

No competing collection, replay, settlement, evaluation, journal, CLI, Factory,
or Plan 6 implementation is introduced.

## Causal admission boundary

The adapter fails closed unless one candidate resolves through a structurally
valid closed campaign to exactly one opportunity, authoritative source receipt,
and reveal-boundary receipt. It then requires:

- exact source identity between point-in-time evidence and a currently authorized
  source-rights receipt checked no later than the immutable decision cutoff;
- `observed <= available <= cutoff <= decision < outcome available <= settled`;
- observation time inside the frozen opportunity interval and opportunity cutoff
  no later than the feature cutoff;
- an ACTION to bind its exact economic decision through a canonical
  `PAPER_PROPOSAL`, or a non-action to bind exact WAIT/NO_BET semantics;
- observed, never simulated, outcome and reward identities after the frozen
  reveal boundary;
- exact terminal action/outcome/reward/transition and settlement/finalization
  identities.

## Outcome-leakage control

Two independent hashes are emitted:

| Hash | Inputs | Future outcome allowed |
|---|---|---|
| pre-decision source evidence | protocol/opportunity, source receipt, observation, feature provenance, source rights | no |
| terminal evidence | frozen campaign root/boundary plus settlement or abstention terminal identities | yes |

Changing a future reward changes the terminal/sample hash but cannot change the
pre-decision source-evidence hash. This preserves source-level leakage detection
without encoding future labels in the source key.

Negative opportunities remain `TerminalEvidenceState.NEGATIVE` with
`observed_reward=None`; the observed reward object is used only to prove causal
terminalization and is never converted into an invented learning reward.

## Reproduction

Focused:

```text
PYTHONPATH=src python -m pytest -q \
  tests/test_forward_campaign_authority_adapter.py
12 passed in 0.40s
```

Cross-authority regression:

```text
PYTHONPATH=src python -m pytest -q \
  tests/test_forward_campaign_authority_adapter.py \
  tests/test_forward_campaign_population.py \
  tests/test_forward_learning_population_binding.py \
  tests/test_forward_evidence_completeness.py \
  tests/test_forward_evidence_completeness_blocking_falsifiers.py \
  tests/test_paper_settlement_learning.py \
  tests/test_paper_abstention_learning.py \
  tests/test_point_in_time_evidence.py \
  tests/test_source_rights_manifest.py
165 passed in 17.41s
```

The focused suite includes 250 repeated pure-adapter materializations and proves
identical canonical payload, sample hash, and learner input after restart-style
replay. Static checks:

```text
git diff --check
python -m py_compile \
  src/autosport/forward_campaign_authority_adapter.py \
  tests/test_forward_campaign_authority_adapter.py
```

## Scientific claim boundary

This block proves causal and identity admission only. It makes no profitability,
edge, promotion, or real-money execution claim. Those claims remain unavailable
without untouched forward evidence and the canonical scientific authorities.

# Account C forward-learning population binding — 2026-10-24

## Outcome

`forward_learning_population_binding.py` closes the seam between the immutable
forward-campaign population manifest and the already canonical provider-free
continual-learning/evaluation inputs. It is an evidence adapter only: it does not
collect, settle, train, evaluate, persist a journal, promote a model, or authorize
execution.

## Reused authorities

- `forward_campaign_population.py` owns causal timestamps, decision cutoff,
  outcome-after-decision admission, source/event/sample leakage protection,
  negative evidence, session chains, regimes, drift bindings, and restart proof.
- `continual_learning_cycle.py` owns `VerifiedLearningExperience`,
  `FrozenPolicySuite`, `ContinualLearningRecipe`, training, rollback, and paired
  retention/holdout evaluation.
- `policy_evaluation.py` owns the evaluation-case schema and reward modes.
- `learning_environment.py` owns action/reward/transition identity and observed
  versus simulated truth.

No code in those authorities was modified. Durable journal/CLI/Factory and Plan 6
paths were not touched.

## Exact binding rules

| Campaign role | Required consumer | Fail-closed checks |
|---|---|---|
| `NEW` action | `VerifiedLearningExperience` | exact sample/source identity, observed reward, decision time, outcome availability, settlement time, reward value |
| `NEW` non-action | negative-evidence member | cannot enter learner and binds the terminal negative-evidence SHA |
| `REPLAY` action | `VerifiedLearningExperience` | same causal and identity checks as NEW |
| `REPLAY` non-action | negative-evidence member | cannot enter learner and remains in the denominator |
| `RETENTION` | `PolicyEvaluationCase` | exact sample/source identity, observed truth, regime, observation and outcome-availability times |
| `HOLDOUT` | `PolicyEvaluationCase` | same checks as retention; exact independent holdout membership |

Every manifest sample has exactly one member binding. Missing, extra, duplicated,
or role-swapped consumers fail before the learner runs. NEW and REPLAY must each
contain at least one observed action, and their combined actions must fit the
frozen recipe update budget. The recipe must name the exact retention and holdout
suite hashes.

The receipt binds the campaign manifest, scientific protocol, recipe, suites,
every member, all role digests, the negative-evidence digest, and a causal
`bound_at`. Canonical JSON restart plus authority replay must reproduce the exact
binding hash.

## Authority boundary

The receipt hard-requires:

- `profitability_claim=false`
- `promotion_authorized=false`
- `real_money_execution=false`

Passing the binding proves population/consumer consistency only. It does not show
profitability, an edge, promotion readiness, or real-money safety. Those claims
remain impossible without untouched forward evidence and their canonical owners.

## Reproduction

Focused:

```text
python -m pytest -q tests/test_forward_learning_population_binding.py
14 passed in 0.29s
```

Cross-authority regression:

```text
python -m pytest -q tests/test_forward_learning_population_binding.py \
  tests/test_forward_campaign_population.py \
  tests/test_continual_learning_cycle.py \
  tests/test_policy_evaluation.py \
  tests/test_forward_evidence_completeness.py \
  tests/test_forward_evidence_completeness_blocking_falsifiers.py \
  tests/test_paper_settlement_learning.py \
  tests/test_campaign_evidence.py \
  tests/test_drift_control.py
169 passed in 101.37s
```

Static checks:

```text
git diff --check
python -m py_compile \
  src/autosport/forward_learning_population_binding.py \
  tests/test_forward_learning_population_binding.py
```

The environment did not have the optional `ruff` module installed; no ruff PASS is
claimed.

# Account C — exact forward-campaign assembly, 2026-10-25

## Scope and current integration state

`forward_campaign_assembly.py` composes existing canonical authorities into one
complete population manifest. It introduces no collection, normalization,
settlement arithmetic, evaluation, training, scheduling service, durable journal,
CLI, Factory, or Plan 6 implementation.

Dependency: [PR #2576](https://github.com/Oleksii-debug/Autosport/pull/2576), frozen
head `425ac6839666ac9020e37cd66a8269ba1ce92ad7`. That PR is implemented but unmerged.
Its exact-head CI ran **11827 passed / 1 failed / 33 skipped**; the failure is the
Plan 6 Python 3.11 `HostilePath._flavour` fixture. Windows packaging failed the
Plan 6 license RECORD gate. Cancelled matrix jobs are not PASS. Account C does
not change either owner boundary or merge on failed qualification.

This is a dependency-safe addition on the exact frozen predecessor, not a
replacement or rewrite of it. Plan 6 convergence remains with its owners and
[PR #2573](https://github.com/Oleksii-debug/Autosport/pull/2573).

## Demonstrated gap

An individually admitted sample is not proof that every frozen candidate reached
the population manifest. A caller could previously omit a NO_BET candidate, or
assign an outcome to HOLDOUT only after observing the result. Composite source
keys also cannot by themselves detect one raw record reused under another
candidate identity.

The assembler closes those composition gaps without replacing any authority.

## Frozen policy and exact coverage

1. Construct `FrozenForwardPopulationPolicy` before the protocol precommit. It
   contains an existing scientific evaluation-policy hash plus the bounded,
   gap-free candidate allocation: sequence, role, session and regime.
2. Bind its `policy_sha256` into the existing
   `ForwardEvidenceProtocolEnvelope.forward_evaluation_policy_sha256` field. The
   existing protocol/anchor remains the commitment authority. A policy timestamp
   by itself is not independent evidence of a real external precommit.
3. Collect and resolve data through existing owners. Supply canonical issued
   source-rights authorizations, point-in-time feature evidence and either
   `PaperSettlementLearningWitness` or finalized abstention authorities.
4. `assemble_forward_campaign` requires the same exact candidate sequences in
   the policy, opportunities, denominator and authority inputs. Missing, extra,
   duplicate or substituted candidates fail closed, including negative members.
5. It delegates every ACTION/negative admission to the #2576 adapter. It builds
   sessions and the manifest through `forward_campaign_population`, preserving
   its event/sample/source firewall, causal cutoffs, negative evidence,
   per-regime counts and predecessor-session hash chain.

The assembler additionally rejects repeated raw source-receipt and immutable
dataset-record digests. Rebranding the same raw record as a different event or
candidate therefore cannot make it an independent evaluation sample.

Only observed ACTION members in NEW/REPLAY export `VerifiedLearningExperience`.
RETENTION/HOLDOUT never export training objects. A finalized abstention remains
in its exact denominator and carries no invented observed reward.

## Restart and authority boundaries

The canonical assembly receipt binds the full population manifest, protocol,
population policy, terminal cohort root, each candidate/opportunity/sample hash
triple and every exported learning-experience ID.

`restore_forward_campaign_assembly` rebuilds the entire assembly from canonical
authority inputs, then compares canonical JSON bytes. Stored counts, hashes or
Boolean flags are not trusted. Restart requires rights reissued through the
existing source-rights authority, not serialized positive authorization objects.

Source-rights receipts prove the configured authorization gate passed; they are
not legal advice or proof of real provider entitlement. Forward structural
receipts do not independently authenticate external source inventories or anchor
timestamps. Session drift hashes remain references supplied by the canonical
drift owner, not newly computed or independently validated drift findings.

## Reproduction

Focused:

```text
PYTHONPATH=src python -m pytest -q tests/test_forward_campaign_assembly.py
23 passed in 6.32s
```

The focused suite includes missing-negative, duplicate/extra/substituted
candidate, role/regime post-selection, late precommit, coercive sequence IDs,
wrong terminal authority, incomplete session, event/raw-source alias, canonical
receipt tampering and authority-reissued restart falsifiers. An integration test
feeds the result into the existing learning population binder with exact frozen
retention/holdout suites and recipe hashes.

The endurance test assembles **20 synthetic sessions / 80 synthetic candidates**,
round-trips canonical JSON, reissues lawful-source fixture receipts and replays
all authorities. It is not evidence that a real forward campaign ran.

A second restart test uses two independent Python processes. The new interpreter
reissues source-rights authorization and replays canonical terminal inputs; no
issuer memory or serialized positive permission from the first process survives.

Deterministic two-session fixture readback:

```text
assembly_sha256=b394c5e8c7537513a8c7ee8c792a5b31e417c9f1b31a9ad61930e46c5c1b3478
manifest_sha256=af90b29ed0a0b2722145a3b718b767fa31bce1840c6c9bfdf2a87e3587e863cc
policy_sha256=386ee11f0d5732a6e39de70d2337cd315664a06641645b7ae8862a99cf92b467
candidate_count=8
session_count=2
negative_count=3
learner_sample_ids=opportunity-1,opportunity-5
fixture_only=true
```

Cross-authority qualification:

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
  tests/test_drift_control.py
```

Final source/test result: **240 passed in 97.67 seconds**. No failed, cancelled
or stale hosted check is described as PASS here.

Static checks:

```text
git diff --check
python -m py_compile src/autosport/forward_campaign_assembly.py \
  tests/test_forward_campaign_assembly.py
```

## Scientific truth

`profitability_claim=false`, `promotion_authorized=false`, and
`real_money_execution=false` remain literal receipt fields. No untouched real
forward outcomes were collected by this code block. No edge, profitability,
whole-product completion, physical NVDA, or Plan closure is claimed.

# Plan 3 / Section 7 — Strategy and Model Factory: terminal reuse qualification

Status: DONE (repository-controllable component engineering; no real-money, model-execution, or physical NVDA claim).

## Fixed acceptance
- 7.1 Reuse `strategy_model_factory`, `champion_policy`, `champion_eligibility`, `policy_deployment`.
- 7.2 Candidate -> independent frozen evidence -> PROMOTE / REJECT / rollback with exact artifacts and predecessor authority.
- 7.3 Reject model self-promotion and stale/future evidence; retain negative/null results and allow durable restart.

## Canonical evidence and no-rewrite decision
- Qualified code ancestor: PR #793 exact SHA `6633da8f0022e1807d56d91daf5033c2997b962b`, already merged, including the complete inherited factory/champion/deployment implementation and tests.
- Readback on main `982bbe0686e00de6bf65874f19f5a496fdf67c5e`: GitHub compare from the tested SHA is ahead=51, behind=0; changed paths are registry/evidence docs and Plan-5 hot-path/performance source/tests only. None of the Section-7 production or test paths changed.
- Production source blobs on main: `strategy_model_factory.py` `f99fe7c63190a2279b14ade79eb8b824004bf2a5`; `_strategy_model_factory_impl.py` `d32a3de50469b33a8ddb365b9bc581b5788f2200`; `champion_policy.py` `7a6e7f58d56f477d513cf1cb2a2554e14cb13e45`; `champion_eligibility.py` `b7af1d11fbb262b6cd72647d22424b80b2c0dfef`; `policy_deployment.py` `8bc37cea934e649404110065912f86dff1a5a6fc`.
- Existing factory exposes frozen causal training, canonical feature/evaluation/promotion-rule identities, exact artifact digests, staged publication/recovery, scientific Registry promotion/negative-result decision and deterministic promotion/rollback authority. Champion activation resolves durable PROMOTE/ROLLBACK history rather than accepting a policy-provided promotion assertion. Eligibility checks exact causal cutoff, drift scope and expiry.
- This is a component qualification of the Plan-3 factory/promotion boundary. Separately open PR #1892 (product executable publication receipts) and PR #1890 (product runtime reconstruction) remain separate, unmerged product-integration enhancements; this closure does not assert those enhancements are integrated, nor that a registered model executes real-money actions.

## Executed tests / falsifiers, exact source identity
- CI run https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611 is SUCCESS for the frozen SHA, including `python -m pytest -v tests` executed successfully on Ubuntu/Windows and Python 3.11/3.12 (all four jobs). Same SHA Windows Candidate build/admission SUCCESS: https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755.
- `tests/test_strategy_model_factory.py` blob `e8c1b9bafa99e03c615c95f3e99106d60de8ddf6`: frozen no-future/reveal training and walk-forward, immutable metric/config, negative/inconclusive retention, holdout one-attempt consumption, promotion guardrails, registry and artifact restart/tamper, drift recommendation without promotion authority.
- `tests/test_strategy_model_factory_preregistration.py` blob `7924ebb91a6a893a02d5f3df18910bd0d338e027`: retrospective preregistration fails before mutation.
- `tests/test_strategy_model_factory_atomic_transaction.py` blob `1d0575e9837162cf51fcddaf0fbad281c47de9a9`; `tests/test_strategy_model_factory_causal_publish_recovery.py` blob `ee911590c4652a2554c4ce4144cbf217e91d9119`: concurrent writers, hostile artifact verification, interrupted publish and exact retry/recovery, frozen final-fit population.
- `tests/test_strategy_model_factory_conflict_preflight.py` blob `4ad2009d3cd7d8f3a8caf603ddfcd384ce1237d5`; `tests/test_strategy_model_factory_promotion_order_preflight.py` blob `644d0afa8759a435b6c6d3b379876edef462ef87`: conflicting candidates and malformed promotion sequence fail before write.
- `tests/test_champion_policy.py` blob `2dc550cc2b49c63a5fc6e2765ee6f0d2cced0e9d`: promotion-required activation, rollback lineage, restart, forged/future/tampered identity, exact action scope, risk-only narrowing.
- `tests/test_champion_eligibility.py` blob `0ad5e0db79e6de19bb6012cac9812660efdc9706`: self-issued drift cannot grant authority, exact lineage and scoped drift, sustained-window evidence, stale/future or widened eligibility denial.
- `tests/test_policy_factory_end_to_end.py` blob `09f665479311a7c7192dfcfe2708129b275d2883`: independent qualified counterfactual witness, materialization receipt, frozen policy re-test, restart, one-use confirmation holdout, forged/revoked/late/future evidence rejection.

## Disposition
All tests above are inherited already-executed exact-SHA suite evidence, not tests freshly executed on a local checkout in this closure run. GitHub source/test readback and commit compare demonstrate unchanged bytes in the closure scope. No feature rewrite, secret, bookmaker credentials, owner/Risk authority expansion, model self-promotion or real-money execution. Physical Windows/NVDA acceptance remains a separate final product gate. Section 7 terminal DONE under AGENTS.md Simplified Section Closure Protocol v3; reopened only on a concrete demonstrated regression.

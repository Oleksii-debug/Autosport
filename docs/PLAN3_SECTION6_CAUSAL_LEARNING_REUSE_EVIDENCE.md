# Plan 3 — Section 6: Causal learning, credit assignment, experiential/RL laboratory

Status: terminal repository-controllable **PAPER/SHADOW/SIM learning-laboratory** closure by reuse of integrated code, 2026-10-09.
Scope: canonical Drive `3. Третій план` §6.1–6.3. Product economic/Risk/promotion permissions are not part of this laboratory.

## 6.1 Complete causal *laboratory* lifecycle (reuse, not duplicate)

| Stage | Existing canonical owner | Exact source Git blob |
|---|---|---|
| OBSERVE / causal identity | `CausalLearningEnvironment` binds source, data, config, seed, protocol, cutoff and causally visible `Observation` | `src/autosport/learning_environment.py` `cedf8e142ef5a93af2f9b1a6a91a2da97f71885c` |
| ACT / admissibility | Externally supplied action set; immutable decision identity, idempotent pending resolution | same environment blob |
| OUTCOME / REWARD | Typed `Outcome`, `RewardEvidence`, `Transition`, `Episode`; separate `EvidenceTruth.OBSERVED` and `SIMULATED`, availability/cutoff and checkpoint | same environment blob |
| ATTRIBUTE | `AgentLoopRuntime` creates durable attribution/research handoff from canonical transition and resolved action, fences UNKNOWN effects | `src/autosport/agent_loop.py` `4c80daf249a0c5d5ea9917b1f1d539b5c232c219` |
| UPDATE (generic lab only) | `BanditPolicyState.update` consumes exact causal action/reward/transition, increments generation and carries immutable IDs/deterministic estimate state | `src/autosport/transparent_bandit_policy.py` `651ef8f8f51a394ca6ccd6df01b8d096004f6ee6` |
| RETEST / governed scientific evaluation seam | `PolicyRetestSpec` and `run_policy_retest` verify exact successor policy/causal witnesses and delegate to existing Strategy/Model Factory and ScientificRegistry rather than publishing independent promotion | `src/autosport/experiential_learning.py` `2f89eb22a301c3413ec2b37e0fdeca50431d9bfa` |
| Product utility authority fence | `attempt_utility_bound_update` refuses raw-reward product mutation while owner-bound utility evidence is unresolved; exact blocked attempt is deterministic and leaves policy unchanged | `src/autosport/policy_update_authority.py` `6b0e09585aca087d524f4d91b33396bee7c135a7` |
| PAPER settlement witness | Read-only, exact settlement/reveal evidence passes via existing PaperSettlementLearningBridge without minting new true outcomes | `src/autosport/paper_settlement_learning.py` `1eba0059c829e9580f5a2811a06b79fe4e0e2d98` |

**Boundary clarification:** Generic causal learning and policy update are available in the lab; this does **not** assert that economically governed product utility updates, promotion or real bookmaker execution are enabled. Missing product utility authority fails closed, not via an ungoverned fallback. Retest and possible promotion have their own frozen independent evidence and admission gates under the existing research factory and later Plan-3 Section 7.

## 6.2 Truth, leakage and authorization falsifiers

- Historical replay cannot transform unfilled/counterfactual simulations into observed outcomes; `EvidenceTruth` identity and model-source evidence remain distinct across serialization and restart.
- Decision cutoff controls causal observations, rewards and transitions. Late or artificially backdated observations/rewards cannot change admitted history.
- UNKNOWN external effects block attribution and no-blind-repeat; exact episode, action, reward and transition identity prevents conflicting replays.
- A learned policy chooses only from an externally issued admissible set, not owner balance, EconomicGoal/Risk/settlement or execution authority.
- No self promotion: a generic updated policy is not a factory/promoted product candidate without independent evaluation and current unconsumed holdout. Product utility-bound gate rejects nongoverned raw reward updates.

## 6.3 Executed negative/restart/memory scientific regression families

The following test blobs are identical on the current main and the frozen executed CI head:
- `tests/test_learning_environment.py` `b24bdadb6766ec9502caf0ea72a8f4186e5a9fe6`: cutoff/future observation, exact source/config/seed, observed vs simulated, unresolved checkpoint, duplicate/changed actions and restart binding.
- `tests/test_agent_loop.py` `fba978267a379f91bc14706e299c00b5ff981777`: full PAPER observe-to-attribution handoff/restart, unknown effects, forged environment/reward, future evidence, payload tampering, duplicate effects and phase/digest preservation.
- `tests/test_transparent_bandit_policy.py` `2501eef5d48db9c2a0e55ae9e2df1a9d0dfed6a9`: deterministic exact update, duplicate/conflicting reward, admissibility and source/config identity.
- `tests/test_experiential_learning.py` `7fd84aa112f2a47f3008daa0daa8948e48eba956`: successor/retest identity, frozen policy config, causal witness, missing owner utility and hostile subclasses.
- `tests/test_policy_update_authority.py` `5decd0479a36ab3be3a436f383438e1f4c2a1189`: bounded fail-closed owner-utility gate, non-mutating attempt, mismatched/rebound authority and hostile types.
- `tests/test_policy_factory_end_to_end.py` `09f665479311a7c7192dfcfe2708129b275d2883`: lab retest/factory integration, independent qualification and holdout, restart, forged/revoked future/backdated/mismatched evidence, materialization receipt and reused confirmation holdout.

## Executed exact-source and post-integration readback

Frozen full qualification: PR #793 head `6633da8f0022e1807d56d91daf5033c2997b962b`.
- [CI 37884352611](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611): **SUCCESS**, Ubuntu/Windows Python 3.11/3.12, complete `python -m pytest -v tests` matrix and `python -m autosport demo`.
- [Windows Candidate 37884352755](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755): **SUCCESS** build/admission.
- Tested PR #793 source integrated to main in `f2f743ff78847d228142af3a4831497197a5cc03`; from frozen tested SHA to main after Section 5 closure, compare ahead=49 behind=0 and 14 touched paths. **None** are any Section-6 source or tests listed above; their main blobs were independently fetched and matched. No local execution in this isolated environment is claimed; the recorded tests are actual hosted exact-head runs on identical source.
- Implementation already integrated; no acceptance-critical source changes identified. Adding competing environment/agent/learning/promotion stores or broadening raw reward financial authority would regress this acceptance.

## Evidence class

Source/fixture/sim/PAPER/SHADOW, never an authenticated bookmaker fill, independent claim of strategy profitability, economic permission, model self-promotion or real-world NVDA test. **REAL_MONEY_EXECUTION=false; HUMAN_TESTED=false; NVDA_VERIFIED=false; WHOLE_PRODUCT_COMPLETE=false**.

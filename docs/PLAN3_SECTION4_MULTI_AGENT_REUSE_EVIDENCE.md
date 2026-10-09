# Plan 3 — Section 4: Multi-agent planner, research roles, independent critic

Status: repository-controlled implementation already integrated; reuse-first acceptance audit, 2026-10-09.
Conflict key: intelligence, research-science, learning-models.
No new model authority, provider action, risk mutation, or financial execution is introduced.

## Existing canonical production roles (all already on main)

| Requirement | Actual integration/authority | Source blob |
|---|---|---|
| Researcher | `ResearchSupervisor` maintains one durable trigger/run, frozen research phases, scientific bindings and restart-safe checkpoints; `ScientificRegistry` owns the scientific record | `src/autosport/research_supervisor.py` `ded56a5e4dd45a7b8194657b41af202419fda9f3` |
| Strategist | `ResearchStrategyPlan` binds typed frozen replay instructions to one canonical source SHA; `ResearchReplayAgent` dispatches once per exact causal quote/time trigger and uses the canonical decision ledger | `src/autosport/research_strategy.py` `7e32f3d10e46e30328c8b2034813dd02c58d6c03` |
| Independent verifier/critic | `DeterministicResearchCritic.review` is a separate injectable class returning typed `CriticVerdict`, checking predecision input cutoffs, evidence hashes, uncertainty, market identity, quality flags and stale quotes without LLM calls | `src/autosport/research_pipeline.py` `c470834f401851b48cfa3a2fc13cb009d4c97fb5` |
| Planner | `ResearchDecisionPipeline` composes critic, proposal/portfolio impact, canonical `PaperRiskPolicy`, `PaperBook`, and decision ledger. It is PAPER-only and cannot mint provider receipts or expand owner limits | `src/autosport/research_pipeline.py` same source |
| Role identities | `AgentOrchestrator` fixes exact ordered agent names and composition SHA; rejects duplicate names, post-bind drift, and peer mutation before later dispatch | `src/autosport/agents.py` `1d4691a47cfdb156b1373799dee5989a15ff4c99` |
| Restart and memory | `AgentLoopRuntime` owns durable causal transitions/attributions and research handoff while `ResearchSupervisor` owns scientific run state; `SkillRegistry` limits procedural calls to source-owned read-only capability | `src/autosport/agent_loop.py` `4c80daf249a0c5d5ea9917b1f1d539b5c232c219`; `src/autosport/skill_registry.py` `08de31b2ee1b7831cab3aac90a978aa9ba3adf18` |
| No-LLM route | `ModelComputeRouter` supports exact deterministic baseline, privacy/budget/deadline-fail-closed WAIT, without dependence on an external model | `src/autosport/model_compute_router.py` `bea69b62f02a620737127ba74e4acf54396f4a33` |

## Existing regression/negative/restart evidence

- `tests/test_agent_composition_identity.py` `4cacc0e16b7ff5310809ce21c2d040dd29fd6a8d`: identity collision, subclass ingress, composition drift during callback/replay, order sensitivity.
- `tests/test_research_pipeline.py` `e68114e1c053f89959076e9cf6592a13739c4dc2`: stale/future forecast rejection, causal evidence hash, independent critic/risk veto, exact ledger conservation and ambiguous-write restart/idempotence.
- `tests/test_research_strategy_runtime.py` `432cf08071b210a84c4d2fff2a93d7000489ec72`: complete replay-to-decision path, stale odds, malformed source identity, reproducible strategy hash.
- `tests/test_research_supervisor.py` `1ab590cdfe9ebf140f9f92a4795255461988aa4e`: restart, trigger conflict, stale checkpoint, forbidden future scientific binding, lifecycle budget/deadline.
- `tests/test_agent_loop.py` `fba978267a379f91bc14706e299c00b5ff981777`: durable cycle, UNKNOWN external effect fence, attribution forged/future evidence rejection and restart/no-repeat.
- `tests/test_skill_registry.py` `da0dca2ae0bfb1354d1ebec3614ff4caec84fcb1`: source-owned read-only profiles, protected mutation denial, restart interrupted run/no blind replay, no promotion/real-money delegation.
- `tests/test_model_compute_router.py` `20d90488796a222e02ac01349dc0d65b738f86cb`: deterministic baseline without model escalation; privacy/deadline/cost gate.

## Executed source-identical integration qualification

Canonical tested snapshot: Plan-3 Section-3 PR #793 head `6633da8f0022e1807d56d91daf5033c2997b962b`.
- CI https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611 — all 4 executed Ubuntu/Windows Python 3.11/3.12 matrix jobs SUCCESS. Each ran exact source checkout verification, `python -m pytest -v tests`, and `python -m autosport demo`.
- Windows Candidate https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755 — build and admission SUCCESS.
- All production and test blob SHAs listed above were read back on `main` and compared to exactly the tested #793 head: **identical**. No changed logic or missing tests in this Section since that run.
- Accepted source landed through PR #793 at `f2f743ff78847d228142af3a4831497197a5cc03`; documented source/test identity checked again after merge. Later commits were registry/other-plan changes, not these modules.

## Acceptance and limits

Section 4 implementation is reused, not replaced. The independent deterministic critic is a separate role, never a financial owner. No-future provenance, immutable agent/plan/run identities, negative/UNKNOWN outcomes, replay/restart, idempotency, and no-LLM deterministic fallback were qualified in executed full CI using identical source/test blobs. The plan does not require a separately invented four-process runtime when composition and independent role authority already exist.

This is **repository/fixture/PAPER scope only**. It is not forward economic-edge proof, live bookmaker execution, a profitable strategy claim, real-money authorization, or physical NVDA acceptance. REAL_MONEY_EXECUTION=false; HUMAN_TESTED=false; NVDA_VERIFIED=false; WHOLE_PRODUCT_COMPLETE=false.

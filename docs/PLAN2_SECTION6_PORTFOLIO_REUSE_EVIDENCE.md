# Autosport — Plan 2 Section 6: whole-session portfolio and endogenous stake-vector

**State:** terminal component qualification by REUSE → QUALIFY → INTEGRATE → READBACK (no production rewrite).  
**Conflict key:** `financial-core / paperbook / portfolio-risk`.  
**Predecessor:** Plan 2 Section 5 is terminally DONE in the live registry and Drive.  
**Integrated source ancestor:** merged PR [#2270](https://github.com/Oleksii-debug/Autosport/pull/2270) head `1f81b7daedb180df4bab22e5877ccea998187f3d`; baseline main `77a4654d633e585ba95923339646d93bb0827b8e`. Compare: ahead 264, behind 0; mapped Plan-2 §6 source/test paths unchanged; Git blobs independently re-read at each ref.

## Acceptance mapping

- **6.1 Whole-session portfolio.** `portfolio_plan.py` provides `OpportunityIntent`, `PortfolioDependencyGraph`, canonical portfolio+candidate vector identities, `PortfolioDependencyEvidence`, `RobustPortfolioProposal`, `TerminalStateCompletenessEvidence`, `VerifiedTerminalEconomics` and a durable `PortfolioPlan`. It evaluates all proposals against existing open positions and the same base PaperBook cut, with `STAKE_VECTOR / WAIT / ZERO` rather than blindly optimizing one isolated bet. `candidate_optimizer.py` uses beam generation solely for discovery; final ranking recomputes portfolio marginal worst-case P&L with the canonical `ScenarioSearchEngine`, uses conservative floor if extrema are unproven and never promotes standalone model EV into permission.
- **6.2 Cash, liquidity, provider/account, risk, correlation, scenario, price constraints.** `risk.py` `PaperRiskPolicy` enforces owner ceilings and reserve, exposure/concentration, bankroll/currency, provider-account bindings, risk-of-ruin vector evidence and immutable PaperBook authority; `economic_goal.py` cannot autonomously enlarge hard owner limits; `portfolio_plan.py` verifies source/quote freshness, accepted-versus-observed authority, complete scenario/dependency witness and conservative correlated stress from a full pair matrix. `_robust_portfolio_quantum_grid.py` floors each positive stake to an exact permitted Decimal quantum; no rounding up of constrained funds. Missing liquidity/executability/accepted-price/provider binding is a constraint/WAIT, not inferred from proposal or ACK. These are deterministic PAPER interface/fixture tests, not live bookmaker liquidity proof.
- **6.3 Explainable, exact, durable allocation.** Monetary values, odds, stake and currency are typed and canonicalized. Vector decisions are reproducible with stable evidence/content hashes, quantized exact-Decimal allocation, explicit zero/headroom and abstention reasons. Post-restart open exposure is included; same decision/intent cannot mint duplicate PaperBook reservations. Under-correlated/over-limit/ruin/stale/future/negative-result cases fail closed, with no learning/model override of Risk/owner authority.

## Immutable source/test identities

| Path | Git blob SHA on qualified head and main |
|---|---|
| `src/autosport/portfolio_plan.py` | `e700dbe49369a32aa9aa3a22d92f6991d6aa06f5` |
| `src/autosport/candidate_optimizer.py` | `bebc2f41ddbab23c12788535341df0ce4073e341` |
| `src/autosport/_robust_portfolio_quantum_grid.py` | `d6797c259203bcdb1cf409033eb35fea300753d5` |
| `src/autosport/portfolio.py` | `4ec9705e7a53a8ce19ad4411ad369b90ed8ad9f6` |
| `src/autosport/risk.py` | `9f092f2e0f8e38a8deb574949ee309cad831f671` |
| `tests/test_portfolio_plan.py` | `7c909d8383eab243e899f051a99f7717c340b8c7` |
| `tests/_test_portfolio_plan_impl.py` | `ffe5096f285daf7224b2d7fd3f9cd2d872dad7fe` |
| `tests/test_economic_goal_endogenous_stake.py` | `319e74861ab0101d0940e0c6b2b74bb6ec5e1efd` |
| `tests/test_portfolio_correlated_exposure_stress.py` | `6f612fed56c14f0b378e936e058333455899d2f4` |
| `tests/test_robust_portfolio_quantum_grid.py` | `bf7de99b9c402cea1dbb9daa00bab9715558f993` |
| `tests/test_candidate_optimizer.py` | `6080011c3fb1780403fd6878d135249027b63b8b` |
| `tests/test_candidate_optimizer_finite_economics.py` | `9dfe2ff352f617374834182a696d20e884b58334` |
| `tests/test_candidate_optimizer_input_determinism.py` | `8dca89f7f3f3fe7a81c32151a0582d4201be88d4` |
| `tests/test_portfolio_settlement_snapshot_integrity.py` | `5accbc0589ee51f53bb6dd9576d96f3cb9ac334d` |

## Executed qualification, integration and boundaries

- [CI 37849986725](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986725): exact `1f81b7da...` completed **SUCCESS**; `python -m pytest -v tests` plus `python -m autosport demo` succeeded in all four Windows/Ubuntu × Python 3.11/3.12 jobs.
- [Windows Candidate 37849986798](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986798): same exact head, **SUCCESS**, package/recovery/UIA/evaluation. The unchanged tests cover multiple candidate stake-vector sizing, context/no-future, Ruin vector binding, provider-account partition, aggregate cap, reserved cash/open exposure, restart idempotence, incomplete/forged/dependency and explicit terminal proof, synthetic/negative scenario math, partial stress, arbitrary money quantum and hostile Decimal context.
- PR #2270 merged main `2c7d233a80257e7c2f23714f55057c8948866d1d`. The evidence document/registry closure changes only audit metadata, not tested executable source. No new post-document full CI claim.
- All outputs are proposed/PAPER-only. A proposed plan is NOT a bookmaker ACK or actual fill. Cash/risk authority is owned by Risk + owner, not by model/research/learning. `REAL_MONEY_EXECUTION=false`. Human NVDA and real provider-account tests remain final/external scope.

**Terminal decision:** Plan 2 Section 6 meets the repository-controllable v3 acceptance boundary using qualified integrated code, with no acceptance-critical missing source feature demonstrated. Terminal `DONE` unless concrete regression invalidates evidence. Plan 2 Sections 7–8 remain separate.

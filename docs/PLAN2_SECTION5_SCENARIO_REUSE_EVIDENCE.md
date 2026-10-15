# Autosport — Plan 2 Section 5: terminal scenario / dependency truth

**State:** terminal component qualification by REUSE → QUALIFY → INTEGRATE → READBACK (no production rewrite).  
**Conflict key:** `financial-core / portfolio-risk`.  
**Baseline:** `main@77a4654d633e585ba95923339646d93bb0827b8e`.  
**Exact tested source ancestor:** merged PR [#2270](https://github.com/Oleksii-debug/Autosport/pull/2270), `1f81b7daedb180df4bab22e5877ccea998187f3d`; main ancestry compare: ahead 264, behind 0; none of the mapped scenario/portfolio source and test paths changed. Source/test Git blob identities were independently read on both refs before this closure.

## Acceptance mapping

- **5.1 Complete relevant terminal states and dependencies.** `scenario_search.py` provides exact bounded enumeration, branch-and-bound with explicit proof flags, conservative fallback and dependency index. `analyse_authoritative` consumes provider-derived terminal-state authorities and refuses to approximate complete truth when state limits are exceeded; a conservative superset remains explicitly non-exact. `market_outcomes.py` is the ownership seam for provider terminal semantics. `portfolio.py` evaluates settled single/multileg ticket net profit from immutable snapshots. `joint_scenario_distribution.py` binds an explicit joint state distribution to one detached PaperBook cut, checks total/marginal probabilities, complete per-group assignments, duplicate identities and before/after authority hashes. `portfolio_plan.py` requires scenario-proof identity for structural arbitrage/dutching/hedge and refuses fabricated positive or future-dependent claims. Parlay is treated as a whole ticket; dependencies are not assumed independent when correlated evidence is absent.
- **5.2 Strictly positive minimum terminal net P&L.** Accepted PAPER odds/stakes/settlement economics run through canonical `Decimal` P&L; a positive lower bound requires a complete, exact, trusted terminal outcome cover *and* executable constraints/cost evidence in the portfolio proposal. Observed sample minimum, theoretical value, or unproven enumeration is not executable guaranteed profit. A claim requiring provider fills is not inferred from ACK, observed quote or a proposed ticket.
- **5.3 Fail-closed incomplete universe.** Missing quote terminal members, inconsistent provider authorities, overlapping terminal assignments, oversize exact state space, bogus marginals/probabilities, stale/tampered bookmaker/PaperBook inputs and pre/post snapshot mutation reject or remain WAIT/conservative. No independent model/learning promotion can mint provider, owner or Risk authority.

## Immutable source/test identities

| Scope | Path | Git blob SHA on qualified head and current main |
|---|---|---|
| Scenario search | `src/autosport/scenario_search.py` | `8f57dba894d9c87e1e3d8a68661024171cae2c78` |
| Joint states | `src/autosport/joint_scenario_distribution.py` | `e3b37b68dad77e26d9e19ebc9a2254ff8033c186` |
| Portfolio terminal math | `src/autosport/portfolio.py` | `4ec9705e7a53a8ce19ad4411ad369b90ed8ad9f6` |
| Portfolio evidence binding | `src/autosport/portfolio_plan.py` | `e700dbe49369a32aa9aa3a22d92f6991d6aa06f5` |
| Exact/conservative scenario test | `tests/test_scenario_search.py` | `52256bd95417299aeb8030476b066b7d30b5dc4a` |
| Snapshot mutation test | `tests/test_scenario_search_ticket_snapshot_integrity.py` | `2783fb368810913dfa380e4286c24ff48c130407` |
| Joint dependencies test | `tests/test_joint_scenario_distribution.py` | `8d2476a35d03e82c469dffd601fe7311b6e1cf9e` |
| Transient ABA test | `tests/test_joint_scenario_distribution_aba_toctou.py` | `2647cdea76cb3e09e4086ec712e424070e476373` |
| Resource/fuzz test | `tests/test_joint_scenario_distribution_resource_limits.py` | `5526546763d14dc644acb24215fa6428b0d8f9c6` |
| Settlement-cut test | `tests/test_portfolio_scenario_snapshot_integrity.py` | `35a878c9a75391b19f12a65ff21c71170d1de8bb` |
| Decimal exactness | `tests/test_portfolio_decimal_context_integrity.py` | `9692710611c18f962e1f93158861788829514c9f` |
| Missing-space falsifiers | `tests/test_portfolio_engine_config_integrity.py` | `11352b755808f7d658aca986cd54f11e049490ce` |

## Executed qualification, integration and boundary

- [CI run 37849986725](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986725): exact PR head `1f81b7da...`, completed **SUCCESS**; full `python -m pytest -v tests` and `python -m autosport demo` successfully executed in all four Windows/Ubuntu × Python 3.11/3.12 jobs. These tests include the listed negative, conservation/economic, malformed, scenario, risk, restart/snapshot and correction families.
- [Windows Candidate 37849986798](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986798): same exact head, **SUCCESS**, including packaged workspace recovery and UIA integration.
- PR #2270 merged to `main` at `2c7d233a80257e7c2f23714f55057c8948866d1d`. Compared tested head to baseline main: ahead-only by 264 commits, no changes in this section's source/test files; independently fetched matching blobs on both heads. This evidence document is an audit/readback record, not an assertion that a separate post-document full CI was run.
- No new monetary effects, no credentials, no provider real fills, no change to authoritative outcomes/owner Risk controls. **PAPER ≠ real**; `REAL_MONEY_EXECUTION=false`; physical NVDA acceptance is separate.

**Terminal decision:** Plan 2 Section 5 meets the repository-controllable v3 acceptance boundary on reused integrated and independently qualified source. `DONE` unless a concrete regression invalidates the evidence. Sections 6–8 are not implied DONE by this record.

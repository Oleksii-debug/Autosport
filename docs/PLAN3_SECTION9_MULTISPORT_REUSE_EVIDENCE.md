# Plan 3 / Section 9 — Multi-sport architecture and two-sport anchor proofs

Status: **DONE** for repository-controllable multi-sport component engineering and lawful **test-only fixtures**. This is not a claim of provider-authenticated samples, live-money action, generalization across every sport, or external validity.

## Scope and implementation reused
- **9.1 Sport-neutral core, sport-specific evidence.** `MarketEvent`, `TicketLeg`, dataset/replay, SQLiteMarketStore, MarketMirror, and PAPER portfolio contracts use canonical sport-discriminated identities. A sport is part of quote, deduplication and settlement keys; legacy ids cannot silently alias a current encoded sport identity. `sport_domain_fitness.py`, `anchor_selection.py`, provider adapters and sport/season participant identity carry sport-specific profile/metric/selection evidence without cloning the core financial or scenario engine.
- **9.2 Two lawful minimal fixture anchors:** `table_tennis` and `soccer`, explicitly marked `TEST_ONLY_ENGINEERING_CONFORMANCE` in `tests/test_multisport_source_store_replay_boundary.py`. They intentionally reuse the *same* local event/market/selection IDs to falsify unsafe cross-sport joins. A three-event fixture includes an earlier and a late soccer correction. Its replay, SQLite persistence, MarketMirror causal `as_of` projections, state recovery, and source-identity/readback checks are deterministic. The test deliberately rejects an attempt to upgrade a test-only fixture into `EXTERNAL_PROVIDER_OBSERVATION` without a new lawful source identity. `tests/test_sport_identity_contract.py` covers cross-sport dataset scope and 20k-event identity bounds, PaperBook sport-bound round-trip and settlement, and sport-aware adapter semantics. `tests/test_research_strategy_sport_evidence_identity.py` tests source/evaluation hash separation on **football and tennis** under identical quote coordinates and binds market competition, semantics, provider class and BACK/LAY evidence. `tests/test_sport_domain_fitness.py` and `test_anchor_selection.py` fail closed on missing/future/simulated/stale evidence and dependent sample duplication; `test_portfolio_plan.py` and the same PaperBook contract provide the shared PAPER portfolio interface.
- **9.3 Minimal architecture proof, not sport-count optimization.** Both anchor streams share domain/storage/replay/scenario/risk primitives, and distinct fixtures validate sport and evidence boundaries. Adding provider verticals is separate provider ownership; existing unmerged sport-provider PRs are **not** portrayed as already integrated.

## Exact-source integration and executed qualification
Canonical tested base PR [#793](https://github.com/Oleksii-debug/Autosport/pull/793), frozen head `6633da8f0022e1807d56d91daf5033c2997b962b`. Main immediately before Section-9 evidence: `058f6ddd07a423e8a7cf1a80e925f7a7b0893763`. The only intervening Plan-3 changes are documentation/status for Sections 4–8; compare from tested base was ahead-only with no changes to the Section-9 paths enumerated here. This is **reuse and source-identical qualification**, not a new test execution or production rewrite.

Production source blobs:
- `domain.py` `006717040c12233d3459dd37e2fd38558f5d12b9`
- `sport_domain_fitness.py` `11f692cf40245e6d335adbd2aa0c13c5024ca0b3`
- `anchor_selection.py` `ea7d3b98f8ffa60bb130be89c387031e30a33212`
- `research_strategy.py` `7e32f3d10e46e30328c8b2034813dd02c58d6c03`
- `scenario_search.py` `8f57dba894d9c87e1e3d8a68661024171cae2c78`

Focused unchanged test blobs:
- `test_multisport_source_store_replay_boundary.py` `eb2c0c29b2bf12f8a6ed4f15827ddc12703f0f7a`
- `test_sport_identity_contract.py` `75bf3eb5473fe818bd3839dd5f15e0081787622d`
- `test_research_strategy_sport_evidence_identity.py` `fcb814a9f4249f709059c033e9162960cc6bf5f7`
- `test_sport_domain_fitness.py` `85c1a5085d1b2c17a84a332ecbe1c3a35dbcac20`
- `test_anchor_selection.py` `2cf89d06301ae522030f8937c6f40419d9e6cfca`
- `test_portfolio_plan.py` `7c909d8383eab243e899f051a99f7717c340b8c7`
- `test_event_lifecycle.py` `e97d33b741a2ae5b81b96f440d1d99ce67d8f324`

[Full CI #37884352611](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611): exact head `6633da8f0022e1807d56d91daf5033c2997b962b`, four **SUCCESS** jobs executing `python -m pytest -v tests` on Ubuntu/Windows Python 3.11/3.12. [Windows Candidate #37884352755](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755): **SUCCESS** build/admission on the same head. The named tests are included unchanged in that executed full suite. PR #793 merged and has zero unresolved review threads.

## Negative, scientific, restart and no-authority findings
- Negative: conflicting duplicate source payload/relabel, cross-sport alias and reserved identity, late correction masquerading as a pre-cutoff quote, sport-market semantic rebind at higher sequence, duplicate evidence clustering and fabricated live authority rejected.
- Causal: as-of before/after late event yields different correct soccer observations, without retroactive contamination; research quote evidence hashes bind sport + concrete market semantics; simulated and future/unknown evidence cannot grant live route.
- Restart: SQLite reopen preserves all three records, sequence and replay dataset hash; PaperBook and sport fitness store round-trip preserve canonical identity.
- Authority: synthetic fixtures do not become real provider observations or money outcomes. Model/learning outputs never authorize Risk, stake, bookmaker writes or self-promotion.

Separate Plan-7/8 external campaigns and physical NVDA evidence remain independent. Source-level Plan-3 Section-9 is complete under Simplified Closure Protocol v3 absent proven regression.

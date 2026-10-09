# Plan 3 — Section 5: Durable strategic, sport, experiment, negative-result memory

Status: terminal repository-controlled acceptance by **reuse of integrated source**, 2026-10-09.
Canonical plan: Drive `3. Третій план`, Section 5 (5.1–5.3).
No new competing memory database, research store, outcome ledger or model authority.

## 5.1 Existing product implementation on main

| Role / owner | Verified existing file and exact Git blob |
|---|---|
| Scientific hypothesis, protocol, experiment, postmortem and frozen outcome provenance | `src/autosport/scientific_registry.py` `5fcfcf96b4240c0111ee33d8710017e3eaa0941d` |
| Deterministic advisory non-positive-result retrieval | `src/autosport/negative_result_retrieval.py` `0bcb5ccd2b98b907dc8e3173b38bbc98aab83c41` |
| Sport/opponent observation, derived ratings/features, as-of snapshots and corrections | `src/autosport/opponent_intelligence.py` `8e27746f1f9c8f2b26b0c2b63752fb10bda04ab8` |
| Durable sport-memory consumption, provenance and cutoff-bound decision view | `src/autosport/sport_memory_runtime.py` `82f2539b2e180e18e861936d292226013d1623b1` |
| Authority generations and independent identity/opponent cross-store checkpoint | `src/autosport/sport_memory_checkpoint.py` `e83f54b354b8621570b9c8602da674b85d535f7e` |
| Source-authoritative settlement-to-memory update, exactly-once results and explicit correction | `src/autosport/sport_memory_result_materializer.py` `8fc4f6ae1363d475e0e423795f4f6fd047cd5ee0` |

## 5.2 Knowledge category and causal boundaries

- **Observation**: canonical outcome/performance and source identities must be resolved from authoritative durable evidence; unverified caller payload cannot mint observed memory.
- **Inference**: ratings, opponent features and matchup evidence are derived views with independent input/cutoff/digest identity, not canonical observed results. Stale/insufficient evidence does not become exact strength.
- **Hypothesis**: ScientificRegistry distinguishes ResearchQuestion, Hypothesis, ResearchProtocol, Experiment, Postmortem and non-positive ResearchOutcome records. Claims and candidate narratives are not accepted observations.
- **Accepted evidence**: registry entries have durable record digests, source/availability timestamps, exact lineage and causal lookup. NegativeResultRetrieval exposes advisory search hits only, not retry, promotion, deployment or financial permission.
- **Expired/stale/future knowledge**: causal as-of filters, source-available-time checks, correction/restatement and decision-bound consumption prevent late disclosure, backdating or later aliases from contaminating historical decisions.

## 5.3 Existing failure/restart/adversarial qualification

Source-identical regression families on current main:
- `tests/test_scientific_registry.py` `392ee1a3880f2190afa64d708e80f3e17392b4c6`: negative memory survives restart, tamper/replay rejection, holdout consumption and no self-promotion.
- `tests/test_negative_result_retrieval.py` `7f8d399609d63cd45099a9ff2297aa2af7cdf722`: causal/restart-stable Unicode search, negative/null/harmful/inconclusive outcomes, deterministic limit/ties, forged/backfilled protocol, chronology and hash rejection, strictly advisory retrieval.
- `tests/test_opponent_intelligence.py` `9005c0515303e239f5cf99d9fd46008241e3dec9`: observed vs simulated, stale/as-of, correction ancestry, deterministic restart, derived-snapshot tampering and publication-failure atomicity.
- `tests/test_sport_memory_result_materializer.py` `e777655f0449fa60f81f44e338999574cf4996f3`: product truth re-resolution, future/backdated event denial, exactly-once restart, identity/provider substitution and win→void correction retirement.
- `tests/test_sport_memory_matchup_decision.py` `5a8d8658bd4f440e6460a942e475b662ac124efc`: frozen historical identity, future evidence rejection.
- `tests/test_sport_memory_checkpoint_concurrency.py` `8a5d9436a88f01a9a7d7b0c07fe89f542f1a1499`: stale concurrent writers/readers reload durable generation.
- `tests/test_sport_memory_consumption_fence.py` `9eba7144b0ab4215acdf47773174fb279396a26e`: stale restart/consumption fail-closed.

## Exact source, execution and integration evidence

- Frozen tested PR #793 head: `6633da8f0022e1807d56d91daf5033c2997b962b`.
- Executed [CI run 37884352611](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352611) **SUCCESS**, Ubuntu/Windows Python 3.11/3.12 all four jobs; full `python -m pytest -v tests` and `python -m autosport demo`.
- [Windows Candidate 37884352755](https://github.com/Oleksii-debug/Autosport/actions/runs/37884352755) **SUCCESS** (build and admission).
- PR #793 is integrated into main via `f2f743ff78847d228142af3a4831497197a5cc03`. Compared exact frozen head to current main `d3d68a66186ea4539f323dabff33718cfab87c76`: ahead 47, behind 0, only 13 changed paths, **none** of the implementation/test paths listed above changed. Current main blob SHA readback therefore matches executed full-suite source; not a claim that current main was independently rerun.
- Reuse-first decision: no acceptance-critical missing module or test discovered; no production code mutation. This document and the closure state are the only new documentation/integration changes.

## Safety and limits

Historical replay, simulated experience, hypothesized result and model inference do not become authoritative observed outcomes. No research/memory module issues financial, Risk, owner, settlement, provider-write or promotion permission. Evidence is source/fixture/PAPER only; **REAL_MONEY_EXECUTION=false; HUMAN_TESTED=false; NVDA_VERIFIED=false; WHOLE_PRODUCT_COMPLETE=false**.

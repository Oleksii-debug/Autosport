# Autosport — Plan 1 Section 8 terminal reuse qualification

Status: **DONE** for repository-controllable engineering. Scope: deterministic replay, causal walk-forward, future-leakage firewall and immutable holdout consumption identity. Closure method: **REUSE → QUALIFY EXISTING EXACT BYTES → READBACK**. No parallel replay engine, no historical-data authority rewrite.

## Fixed acceptance / existing implementation

Plan 1 Section 8 requires reuse of `replay`, causal features, evaluation universe and point-in-time authorities; deterministic replay with exact causal cutoffs, hidden future, provider/source availability and restart; immutable walk-forward and holdout consumption identities.

- `src/autosport/replay.py` implements `ReplayEngine` and `ReplayLeakageFirewall`; history is ordered by availability and immutable dataset identity, with rejected future evidence.
- `src/autosport/causal_features.py` checks feature availability, UTC cutoffs, monotonic availability transforms and train-only standardization.
- `src/autosport/evaluation_bundle.py` enforces governed-dataset and immutable forecast/cohort identities; rejects mismatched observed quotes, backdated inputs, unsupported outcome reveals, incomplete cohorts, cross-window training leakage and malformed JSON.
- `src/autosport/evaluation_universe.py` and `dataset_snapshot_lineage.py` preserve immutable evaluation members and historical snapshot provenance; `historical_snapshot.py` provides source/cutoff evidence.
- `src/autosport/point_in_time_evidence.py` enforces feature/dataset availability before decision cutoff and append-only canonical holdout consumption records with freshness and restart validation. Holdout access evidence is not renewable by renaming or reloading the same physical corpus.
- Existing test families: `test_replay_*`, `test_walk_forward_*`, `test_governed_walk_forward_binding.py`, `test_evaluation_universe_*`, `test_point_in_time_*`, `test_holdout_*`, `test_causal_features.py`, `test_dataset_snapshot_lineage_*`, `test_historical_snapshot_*`, `test_benchmark_replay_event_driven.py`. These include chronology, no-future, late availability, source/provenance identity, deterministic restart, corruption/shape/forged-authority rejection, immutable untouched windows, delayed labels, holdout reuse/replay resistance and crash-retry tests.

## Exact-byte comparison and machine qualification

Previously accepted frozen head (PR #2261): `d93d203e867c71aad880f1b2e3d144549e166030`. After Plan 1 Section 7 closure, GitHub recursive Git-tree readback checked `main@c84ae7096b738651d884069b46c9fcd1a6efabc3` against this frozen head: **48/48 scoped Section-8 production and test Git blobs are byte-identical**, with zero divergences. No new source code is required by the fixed Section-8 acceptance contract.

Representative exact blobs (identical on both refs):
- `replay.py` `b79fdbb16d1b78392217bf52b2c7946a3c4c05fb`
- `causal_features.py` `8cb3038dbbcd48ffd13c264879901ff21d280c33`
- `evaluation_universe.py` `99d9f5f712dfc3c8b8f18e1be0317062ef0ea5a6`
- `evaluation_bundle.py` `545a1ab95fdb041c603cf6e452afe5cbdcd8a253`
- `point_in_time_evidence.py` `2505684b89a78e8fd5a9ceee442af0511d3a83db`
- `tests/test_replay_firewall_reuse.py` `931845af3aaabd874e37d0e7783174029fe5a143`
- `tests/test_walk_forward_untouched_window_protocol.py` `cd5bbfd2fc22fd8faebccb6e3551731ce616081f`
- `tests/test_holdout_consumption_adversarial.py` `ca03a713ce107ae0de64fa34c960c55f3607c521`

Executed immutable-head CI: https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018 — COMPLETED SUCCESS, `python -m pytest -v tests` plus demo succeeded on 4/4 Ubuntu/Windows × Python 3.11/3.12 jobs with exact pristine source checkout. Windows Candidate: https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047 — COMPLETED SUCCESS at the same head. The evidence is transferred only by exact blob identity; it is **not** a claim that CI was re-run on this documentation-only registry commit.

## Boundary

Section 8 engineering uses repository-owned fixtures and governed recorded data, not real bookmaker accounts or real-money execution. Fixture holdout tests are not a claim of a genuinely untouched future owner-operated live campaign. Product-level external/live/NVDA acceptance remains separate. `REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`. Later proved regression, invalid evidence, changed contract or breakage requires an explicit `REOPENED` record.

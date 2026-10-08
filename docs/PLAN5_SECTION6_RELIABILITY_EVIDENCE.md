# Plan 5 / Section 6 — reliability, endurance, recovery and backup/restore evidence

Contract: PLAN5_SECTION6_RELIABILITY_V1
Evidence class: REPOSITORY_SOURCE_AND_HOSTED_SYNTHETIC_TESTS_ONLY
Plan authority: Drive `5. П’ятий план` + `MULTI_PLAN_CLOSURE_STATE.md`.
Previous terminal Section 5 is immutable and was not reimplemented.
No real bookmaker account, money movement, live provider, owner-PC soak, or physical NVDA test.

## Reuse / implemented boundaries

The current product already has one canonical operational and recovery path:

- `src/autosport/endurance.py` and `docs/ENDURANCE_STRESS.md`: deterministic generated provider events -> ingestion -> market bus -> SQLite store -> replay, duplicate-stream zero second ingestion, clean-workspace replay equivalence, repeated reopen, synthetic PaperBook ticket settlement and re-open.
- `src/autosport/recovery.py`, `src/autosport/run_registry.py`, `src/autosport/run_transaction.py`, `src/autosport/workspace_lock.py`: existing durable transaction/workspace identity, exclusive writer protection, failed/partial state reconciliation and deterministic continuation.
- `.github/workflows/endurance.yml`: separate Ubuntu and Windows runners; `AUTOSPORT_SOURCE_SHA` preflight through `scripts/verify_source_checkout.py`; bounded 20,000-event/2,000-key, 3-restart, 50-ticket endurance profile, and composed collector retention/backpressure/reopen regression tests. The workflow uploads one JSON artifact per OS. No full product workspace is deleted or reused; it creates a dedicated synthetic fixture workspace.
- Existing snapshot persistence, fail-closed corrupt-state rejection, restored history identity, replay lineage and no duplicate PAPER settlement are the repository-controlled restore/backup guarantees. No claim is made that a tested **external off-machine backup service** or a real-account remote disaster restore exists; those are separate final product/infrastructure claims.

## Real executed machine evidence (not an admission-only SUCCESS)

- Exact source revision: `e2d3ebc7b78807cb5aa46758eca67a044d6949ec`.
- Executed workflow: https://github.com/Oleksii-debug/Autosport/actions/runs/37738857644 — `completed/success`, with `endurance (ubuntu-latest)` SUCCESS and `endurance (windows-latest)` SUCCESS, not SKIPPED. On each OS, the exact source preflight, 20k event bounded endurance, collector composition gate, and artifact upload steps all concluded SUCCESS.
- GitHub artifact digests:
  - `endurance-Windows`: `sha256:d37cfc45a25848f0a097b985641804def8af30b787a9564e153230c7e8b03c0b`.
  - `endurance-Linux`: `sha256:f463740cc543fd9468458655fccbc88dd23af0b68dc460ba5fa9601a4c6d55e3`.
  Both artifacts were listed as not expired when checked. These are upload-archive digests; they do not substitute for directly reading each JSON field.
- Additional complete cross-platform Python suite: https://github.com/Oleksii-debug/Autosport/actions/runs/37778840621 — four actual Ubuntu/Windows 3.11/3.12 test jobs SUCCESS at `eeee53bcf34fc001e740d579d6b6df7f9760f730`. The Section-4 QA ledger records the completed test counts.

## Main versus tested-source identity readback

The following exact source and test blob SHAs were byte-identical between Endurance green source `e2d3ebc7...` and main during Plan-5 Section-6 audit:

| Source/test | Git blob SHA |
|---|---|
| `src/autosport/endurance.py` | `56538df8ba4abf4a1f27b12f5b9b061be3b900d5` |
| `src/autosport/recovery.py` | `f2aca557c60aa8dbbb1f0d184fbc6bfea0c30496` |
| `src/autosport/run_transaction.py` | `94af71706ce176ef1c4cb960cc8fce2555497dcf` |
| `src/autosport/run_registry.py` | `0594923b5e161ed462273f3f4f9b0dd2b0df8091` |
| `src/autosport/storage.py` | `9c39bd6f0a7e95a23946f881402c954286dbe333` |
| `src/autosport/workspace_lock.py` | `ea87fd97d3314eda24464693125d8622653587a8` |
| `tests/test_endurance.py` | `1f47c569c731df78f698881cc0b092a434d98758` |
| `tests/test_collector_endurance_composition.py` | `5a3e22624efbd60ce8ab2290a5422a02b930e1e5` |
| `tests/test_recovery_reconciliation.py` | `752bc205019171c624f8d9f7f588170c397a75f3` |
| `tests/test_transaction_recovery.py` | `ea1235e8d3c6f7cd397bc83ecc212f6b30cbd36e` |
| `tests/test_execution_stop_process_kill_recovery.py` | `f13f14f0a43960978e37311fd0f9c076e3757397` |
| `tests/test_paper_campaign_process_kill_recovery.py` | `23ded18039738bbb2f6cf892a758da57f8e6c43b` |
| `tests/test_endurance_resource_probe_integration.py` | `3c8ed0ac238b35c93c0d017e6d5630ba1c06af6a` |
| `tests/test_endurance_source_preflight.py` | `b3f17770b22767ff261c50964dcf075a4fcbad0c` |

## Fault injection, bounded performance and no-duplicate recovery

Executed existing test families target mid-transaction crash, process kill, torn publication, repeated restore, corrupt/absent registry, conflicting durable state, source-clock chronology, retention/reopen and active-writer races. Endurance produces replay hashes and a stable invariant fingerprint separately from observed throughput/peak memory; it **does not claim a universal production latency budget** (Plan 5 Section 7 owns performance qualification). PAPER settlement replay is idempotent; no synthetic ACK is treated as an external fill; UNKNOWN external effects are not blindly retried.

The deterministic fixture covers logical continuation and restart under bounded load. It does not prove a literal multi-day physical soak on the owner PC. Whole-product prolonged campaign is Plan 7 and final physical/owner acceptance is Plan 8, as indexed by the canonical plan.

## Terminal decision

Required repository-controllable recovery/endurance architecture and test fixtures already exist; no duplicate recovery authority or new effect path was needed. Executed cross-OS Endurance + full regression CI, exact source/fixture readback and explicit non-claims qualify the existing engineering under `AGENTS.md` Simplified Section Closure Protocol v3. A later demonstrated production regression reopens only the failed surface.

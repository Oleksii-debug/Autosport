# Autosport progress — evidence-based 0–100%

Progress is whole-product readiness, not percentage of planned files. Documentation/scaffolding gets little credit; runnable, integrated, packaged and physically verified behavior gets most credit.

## Weighted model

| Area | Weight | Current | Evidence now |
| --- | ---: | ---: | --- |
| Product architecture, contracts, reuse governance | 8% | 4.0% | Canonical product vision, technical project, AGENTS/reuse/accessibility contracts exist. |
| Market ingestion + canonical event model | 14% | 0.5% | Domain implementation starts in bootstrap branch; no real provider yet. |
| Event history + causal replay | 14% | 0.5% | Initial deterministic replay implementation/tests start in bootstrap branch; persistent high-rate store not yet integrated. |
| PaperBook + settlement | 10% | 0.3% | Initial exact-decimal ledger implementation starts; no complete settlement matrix/persistence yet. |
| Portfolio/exposure/combinatorial engine | 20% | 0.0% | Architecture specified; implementation/benchmarks not yet complete. |
| Agents, model gateway, learning/evaluation | 10% | 0.0% | Roles/contracts specified; runtime not yet integrated. |
| Accessible Windows/WebView user experience | 10% | 0.5% | Initial semantic copyable shell starts; no packaged Windows acceptance yet. |
| Persistence/recovery/performance/providers | 7% | 0.0% | Candidate technologies researched only. |
| Packaging, CI, release, physical NVDA QA | 7% | 0.0% | Not yet proven. |

**Whole-product progress at bootstrap: 5.8%** after the branch's initial code/docs land and tests pass. Until those tests/PR are verified, treat live-main progress as approximately **4%**.

## What is ready

Independent public repository and canonical product direction; Google Drive project folder; paper-only/live-analysis scope; whole-product version strategy; copyable keyboard/NVDA UI contract; reuse-first policy; initial storage/analytics research.

## What is not ready

No production data provider, no measured high-frequency store, no complete replay campaign, no persisted PaperBook, no portfolio solver, no forecasting/agent runtime, no strategy learning loop, no Windows packaged executable, no physical NVDA acceptance, no v0.1 release.

## Reporting rule

Every substantial run updates this file only when evidence changes. Do not increment progress for comments, duplicate CI, planning-only PRs or unintegrated speculative code. A branch may have a higher provisional score than `main`; final user-facing score should distinguish provisional branch evidence from integrated evidence.

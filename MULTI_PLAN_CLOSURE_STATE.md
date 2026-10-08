# Autosport — Multi-Plan Closure State

This is the live status authority for the new architecture.

## Rules
- Read PROJECT_PLAN_INDEX.md and MULTI_PLAN_PARALLELISM_CONTRACT.md.
- Plans 1–6 have no global earliest Section.
- In an assigned plan, skip DONE and take the first ACTIONABLE unfinished Section.
- External WAITING states are skipped without false DONE.
- DONE is terminal under Simplified Section Closure Protocol v3.
- Old SEQUENTIAL_CLOSURE_STATE.md is audit-only.

## Migrated accepted / active state

### Plan 1 — Data / causality / replay / outcomes
| Section | State | Note |
|---:|---|---|
|1|DONE|legacy Section 2; accepted main evidence|
|2|QUALIFYING|legacy Section 3; canonical PR #2238 current head `a1bf6ca00dfcc67eb171defedb056cabd3d9a3ed` (negative chronology test repair in two files, production unchanged); prior exact-head CI 37699969910 FAILED (101 failed), Windows 37699969821 CANCELLED, Endurance 37699969904 CANCELLED; new exact-head CI 37728854272, Windows 37728854321, Endurance 37728854316 QUEUED (NOT PASS). Still no DONE: repair remaining causal/replay/projection regressions, qualify all gates, integrate and read back.|
|3|PARTIAL_EXISTING|legacy Section 4; canonical stacked PR #2261@`8a814cf0c1b80df0d0bf28deae4562222762111d`, based on older Section-2 candidate; exact-head CI 37658703334 FAILED (346 failed), Windows 37658703336 CANCELLED; no DONE. After Section 2 terminal integration, reconverge same lineage, qualify source rights/provenance/coverage/corrections/retention/restart and read back.|
|4|PARTIAL_EXISTING|legacy Section 6 storage/history/checkpoint implementation|
|5|PARTIAL_EXISTING|legacy Section 14 collector/sync implementation|
|6|PARTIAL_EXISTING|legacy Section 15 lifecycle/memory implementation|
|7|PARTIAL_EXISTING|legacy Section 16 market-mirror implementation|
|8|PARTIAL_EXISTING|legacy Section 17 replay/walk-forward implementation|
|9|PARTIAL_EXISTING|legacy Section 18 outcome/correction implementation|
|10|OPEN|plan qualification|

### Plan 2 — Financial / risk / paper / portfolio
| Section | State | Note |
|---:|---|---|
|1|DONE|2026-10-08 terminal: Decimal/PaperBook strict ingress PR #2262 merged `f034c670195b587ba560e5d59c58ea1559d27244`; settlement outcome-snapshot/correction/evidence-integrity PR #2266 exact head `ab79c4ec73aa0bbc4ab56253eadbd1ccca8e8c3c` CI `37749881926` SUCCESS (Ubuntu/Windows, 3.11/3.12), Windows Candidate `37749881794` SUCCESS, zero review threads, merged `1ef4765ad419d7159c2892b7e38cdf366b645488`; post-merge readback continuous_session.py `367b7c528ced00163fe684e95f9389e735cce8ca`, tests/test_plan2_section1_outcome_snapshot_isolation.py `371066609727be92df8135baa31258d0f17a52f1`. Automated PAPER conservation, duplicate-ID conflict, crash/restart, correction, durable payload and learning/provider mutation falsifiers passed in exact-head CI. No real-money or physical NVDA claim. DONE is terminal absent documented regression.|
|2|DONE|2026-10-08 terminal Section 2: canonical EconomicGoal/Risk owner-authority PR #2268 exact head `9ca7427ed8db781f64f838f95a2eaf35693dcf61`; CI `37769052587` SUCCESS (Ubuntu/Windows Python 3.11/3.12), Windows Candidate `37769052667` SUCCESS; zero unresolved reviews. Merged into `main` as `22ac879da3af8885692d903459d52e9144bce37e`; post-merge `economic_goal.py` blob `8e9fb21973bbfd1909f62e64a4452321be2d58ee`, focused adversarial/restart tests blob `96ef7d2c7f527e3a6a3443b718b92680eba45dcf`. Exact Decimal, hostile subclasses, postconstruction mutation, ZERO/WAIT, owner ceiling and emergency-stop non-expansion, restart typed snapshot and existing risk/portfolio/PaperBook battery qualified. PAPER is not real; models never expand Risk/owner permissions. DONE terminal absent demonstrated regression.|
|3|QUALIFYING_NOT_DONE|Canonical Plan-2 Section-3 finisher PR #2270 (`plan2/section3-opportunity-parlay-language-20261008`), frozen exact head `27d605645672bbfac331c70d70ceda281595aa8e`. Existing Opportunity/PortfolioPlan/PaperBook/risk language reused; adds PARLAY with complete forecast-leg coverage, read-only exact-currency proposed PortfolioDelta bound to owner Risk/plan/book identity; proposed PAPER stake/cash/commitment only, NOT provider fill or realized P&L. Added deterministic, adversarial/malformed, ZERO/WAIT, multi-vector, Unicode restart, stale-source/late-settlement, precision/conservation tests. Exact-head CI `37783126623` and Windows Candidate `37783126766` QUEUED / NOT PASS; candidate frozen. Require successful applicable gates, safe main merge, post-merge source/test readback and registry+Drive DONE. No bookmaker credentials, external financial authority or physical NVDA.|
|4–7|PARTIAL_EXISTING|Legacy 18–21 and current PaperBook/portfolio/scenario/financial-reconciliation code; each requires its own qualification and terminal readback.|
|8|OPEN|Plan-wide financial/PAPER qualification.|

### Plan 3 — Intelligence / research / learning / multi-sport
| Section | State | Evidence / next gate |
|---:|---|---|
|1|QUALIFYING_NOT_DONE|Frozen promotion protocol repairs merged via #2264 at `179dddba10503d3a8a34de79157766045d0afd8a`; exact-head CI `37737367000` SUCCESS (Ubuntu/Windows) and Windows candidate `37737366994` SUCCESS; post-merge scientific registry blob `5fcfcf96b4240c0111ee33d8710017e3eaa0941d`. Multiplicity family-close #1888 merged at `f04ed3512f163d0e83d84750ea4d699c4cbf349d`, CI `37726876194` SUCCESS and Windows `37726875859` SUCCESS at its PR SHA, post-merge family-close blob `afa592a6bb03ea1f38544881520a850844edf8a2`. **NOT DONE:** holdout physical-content #819 exact-head CI `37726628662` FAILED; disclosure/export #925 exact-head CI `37726795061` FAILED (including monotonic workspace root conflicts). Repair/requalify those existing lineages, verify integrated holdout consumption and negative-result preservation, run combined exact-main scientific/restart/causal regression, then terminal readback before DONE. Source/PAPER/real authority remains separate; research/model cannot extend Risk/owner authority.|
|2|PARTIAL_EXISTING / NEXT_AFTER_SECTION1|Universal Opportunity Engine: existing `opportunity.py` and opportunity contract/authority/revalidation tests must be audited and qualified only after Section 1 reaches terminal DONE; do not claim opportunity or financial execution authority from fixture outputs.|
|3–9|PARTIAL_EXISTING|Legacy Sections 23–29,31 and current research/agent/model/memory code; sequential closure within Plan 3 only.|
|10|OPEN|Plan-wide qualification.|

### Plan 4 — Bookmakers / execution engineering
| Section | State | Evidence / gate |
|---:|---|---|
|1|DONE|Terminal 2026-10-08. Reused canonical Booker capability/governance registry #496, lifecycle #1270, strict JSON #2265 and existing integration-boundary code. Exact provider/interface/account/version + separately scoped terms/legal metadata, and environment/application/endpoint-operation/market/sport capability evidence are covered by matrix #1359@d30022a2a7091f6753ce17afede3be3a677bbfac (8 additive paths). Exact-head CI #37769707056 SUCCESS (Ubuntu/Windows Python 3.11/3.12), Windows Candidate #37769706432 SUCCESS; zero unresolved review threads; merged to main c79985faab84290296ed0a0db42bebd3ca3516b0. Scoped multi-provider account/adapter isolation, UNKNOWN/UNSUPPORTED fail-closed, terms separation, duplicate idempotence and Unicode-path restart test #2269@3d7f89de43e4fff7d18685c8d4205db264128330: CI #37775363869 SUCCESS (all four jobs), Windows Candidate #37775363861 SUCCESS, zero unresolved review threads; merged main 789ffb952ce91b7fede25c4fe6c4aa7075ebffad. Post-merge main readback matrix blob 2f82bbe4df8e39dd51149a38b2159456ba223624 and test blob 39506e8867b92ec607e3edab745649bd7b5dbfe9. Offline/fixture evidence only; technical/governance evidence is not provider write or real-money authority. DONE terminal absent demonstrated regression.|
|2|QUALIFYING_NOT_DONE|Canonical read-only Betfair transport redaction PR #2267@d24eb0c710e56a8e2b3842301a83e8a48da25f3c; exact-head CI #37775505899 and Windows Candidate #37775505789 not yet terminal at Section 1 closure. Merge only after exact-head gates, recheck parsing/pagination/freshness/account-shape/BETDAQ tests, current-main integration and source/test readback. No implicit retry, credentials or external money effects.|
|3–7|PARTIAL_EXISTING / ACTIONABLE_OFFLINE|Legacy 34–37 and existing provider/execution/browser code; sequential inside Plan 4.|
|8|OPEN|Offline plan-wide qualification.|
Real account/money evidence is NOT required for repository-controllable Plan 4 engineering.

### Plan 5 — Runtime / security / reliability / QA / performance
| Section | State | Evidence / next gate |
|---:|---|---|
|1|DONE|Canonical runtime START/STOP/recovery and one-active-lease authority reused; secret-bearing secondary exception notes repaired in PR #2263 (`a909c552d3766657ab8b310c1cb00e99be886388`). Exact-head CI 37726753316 SUCCESS (Ubuntu/Windows Python 3.11/3.12; Windows 3.12 7828 passed, 14 skipped), Windows candidate 37726753340 SUCCESS; no review threads; merged `bb581b82b48ff23a7930e57733c1fbc641245231`. Post-merge product-runtime/test blobs `803c9ec1cb1324ae8b99e19e01e63ffa8092889f` / `be19b16b889de22767715ca080afee94dee15f36` verified. No real bookmaker/financial authority or physical NVDA claimed.|
|2|DONE|Terminal Plan-5 Section 2: canonical secret-canary scanner PR #1694 exact head `3624dedb05429139c9a1e7358963a16eb4402cf3`; CI 37777291415 SUCCESS (four executed Ubuntu/Windows Python 3.11/3.12 test jobs plus admission), Windows Candidate 37777291553 SUCCESS (build, packaging/security/UIA smoke), zero review threads. Expected-head merge into `main` `0e296bdf0ad18d229a8180e6f83d18fe84762f6c`; post-merge scanner blob `3732b965af942512217f01d9cba775d5a2e47a2d`, serialization/adversarial tests blob `16b3f3e42998e9da25d4f3f5a38b4e19054b6730`. Fail-closed race/TOCTOU, UTF-8/UTF-16/URL/JSON/folded-Base64 leak scans, 64k bounded regression and Windows file identity normalization covered. Scanner emits digests/error types, not planted secret. Fixture/source evidence only, no real money or physical NVDA claim; DONE terminal absent demonstrated regression.|
|3|PARTIAL_EXISTING|Canonical forensic journal PR #1198 (`sol/forensic-lock-continuity-v6`); current live SHA is the PR head, not this snapshot. Same five journal/entrypoint/test paths; added fail-closed 64 MiB verified-history capacity cap and oversized-file/crash-restart falsifiers. Full exact-head CI and Windows candidate pending execution, not PASS. May close only after Section 2 terminal DONE, successful tests, merge and readback. Observational evidence only.|
|4–8|PARTIAL_EXISTING|Legacy 39–42 and current QA/verification/reliability/performance/Windows-lab architecture; each separately qualifies.|
Section 9: OPEN plan qualification.

### Plan 6 — Windows / accessibility / packaging / UI
Sections 1–6: PARTIAL_EXISTING from legacy 12,38,43 and current Windows/WebView2/accessibility/release code.
Section 7: OPEN plan qualification.

### Plan 7 — Provider-free PAPER/SHADOW convergence
Sections 1–7: WAITING_UPSTREAM until terminal Plans 1,2,3,5,6 plus terminal Plan 4 Sections 1,2,7 (capability registry, read-only/provider path, public web-lab). Other Plan-4 execution Sections are not M1 blockers.

### Plan 8 — External / real execution / NVDA / final
| Section | State |
|---:|---|
|1|WAITING_EXTERNAL_PLAN4|
|2|WAITING_OWNER_UPSTREAM|
|3|WAITING_SECTION2|
|4|WAITING_OWNER_SECTION3|
|5|WAITING_PLAN6_M1_EXTERNAL_SCOPE|
|6|WAITING_SECTION5|
|7|WAITING_CLAIMED_SCOPE|
|8|WAITING_SECTION7|

Update this file whenever a new-plan Section closes/reopens or becomes externally actionable.

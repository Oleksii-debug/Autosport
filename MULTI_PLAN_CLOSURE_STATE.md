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
|1|QUALIFYING|Canonical existing PR #2262 @ `eb670396bc8573da721aadb83948250a73b39cae`: strict exact Decimal odds/probability/PaperBook bankroll and stake ingress; targeted adversarial and save/restart/dedup tests added. CI #37729887384, Windows candidate #37729887411, Endurance #37729887366 are QUEUED (NOT PASS). PR remains open; exact-head qualification, full Section-1 settlement/correction acceptance audit, integration and post-merge readback pending. No terminal DONE.|
|2|PARTIAL_EXISTING|Next actionable only after Section 1 terminal DONE; reuse EconomicGoal/Risk authority, owner controls, restart and portfolio-risk tests. No independent Section-2 closure claimed.|
|3–7|PARTIAL_EXISTING|Legacy 10,18–21 and current PaperBook/portfolio/scenario/financial-reconciliation code; each requires its own qualification and terminal readback.|
|8|OPEN|Plan-wide financial/PAPER qualification.|

### Plan 3 — Intelligence / research / learning / multi-sport
Sections 1–9: PARTIAL_EXISTING from legacy 11,23–29,31 and current research/agent/model/memory code.
Section 10: OPEN plan qualification.

### Plan 4 — Bookmakers / execution engineering
Sections 1–7: PARTIAL_EXISTING / ACTIONABLE_OFFLINE from legacy 13,33–37 and current Betfair/BETDAQ/provider/execution/browser code.
Section 8: OPEN offline qualification.
Real account/money evidence is NOT required here.

### Plan 5 — Runtime / security / reliability / QA / performance
| Section | State | Evidence / next gate |
|---:|---|---|
|1|DONE|Canonical runtime START/STOP/recovery and one-active-lease authority reused; secret-bearing secondary exception notes repaired in PR #2263 (`a909c552d3766657ab8b310c1cb00e99be886388`). Exact-head CI 37726753316 SUCCESS (Ubuntu/Windows Python 3.11/3.12; Windows 3.12 7828 passed, 14 skipped), Windows candidate 37726753340 SUCCESS; no review threads; merged `bb581b82b48ff23a7930e57733c1fbc641245231`. Post-merge product-runtime/test blobs `803c9ec1cb1324ae8b99e19e01e63ffa8092889f` / `be19b16b889de22767715ca080afee94dee15f36` verified. No real bookmaker/financial authority or physical NVDA claimed.|
|2|QUALIFYING|Existing security/secret_redaction/session/trusted-runtime/owner authority reused; three-file canary leakage falsifier converged in SAME PR #1694 at frozen `e8844826d95b78bfb8b323b0df9bb382630d7c69` (parent current main `b6587daceeaad9cc0342ea5f6ecd99e9846dad2e`, only three scanner/test paths, behind=0, no review threads). Exact-head CI 37734730008 and Windows Candidate 37734729993 QUEUED NOT PASS; draft-triggered skipped jobs are not evidence. Must qualify exact scanner head, merge expected SHA, verify readback then terminal DONE. No financial or external execution authority.|
|3–8|PARTIAL_EXISTING|Legacy 8,32,39–42 and current forensic evidence/QA/verification/reliability/performance/Windows-lab architecture; each separately qualifies.|
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

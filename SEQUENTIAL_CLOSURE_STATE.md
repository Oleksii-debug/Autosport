# LEGACY MONOLITHIC CLOSURE REGISTRY — SUPERSEDED FOR WORK SELECTION

Do not use this file to choose the next work front.
Use PROJECT_PLAN_INDEX.md + MULTI_PLAN_PARALLELISM_CONTRACT.md + MULTI_PLAN_CLOSURE_STATE.md.

Historical migration:
- former 0–1: accepted product/control baseline, retained as audit authority;
- former 2 -> Plan 1 / Section 1 DONE;
- former 3 -> Plan 1 / Section 2 QUALIFYING, canonical PR #2238;
- former 4 -> Plan 1 / Section 3 PARTIAL_EXISTING, PR #2261;
- later Sections map via LEGACY_46_TO_MULTIPLAN_COVERAGE.md.

---

# Sequential Closure State

This file is the durable GitHub mirror for ordered Section/Subsection closure.

## Binding closure lifecycle v2

This registry obeys the root `AGENTS.md` **Terminal Section Closure Protocol v2**. The following invariants are mandatory when selecting or updating a front:

- **Closure is the optimization target.** Commit/PR count, execution-unit floors, depth targets and elapsed worker time do not justify additional mutation.
- **One mutation front.** Mutate only the earliest actionable unfinished Section, except for a minimal named direct dependency required to close it.
- **Audit existing first.** If acceptance-critical implementation already exists, qualify/close it instead of rebuilding or expanding it.
- **One canonical finisher.** Record/reuse one Section finisher lineage; intermediate feature/integration/prequal merges are not closure.
- **Candidate freeze.** Once internally controllable acceptance requirements are satisfied, designate and freeze an exact candidate SHA. No unrelated hardening or speculative edge-case work after freeze.
- **Exact-SHA qualification.** Pending CI freezes the candidate; it does not authorize a new SHA. A failed gate permits only the smallest proven gating repair before refreeze.
- **Integration then readback.** DONE requires required canonical integration plus post-merge/readback evidence, not merely an intermediate merge.
- **External-only remainder.** Use `INTERNAL_DONE_BLOCKED_EXTERNAL` when internal scope is exhausted and a genuinely external fact remains. This is not DONE, but the frozen Section becomes immutable for autonomous sequencing until the unblock condition changes.
- **No reconvergence carousel.** Do not repeatedly propagate a moving predecessor into later Sections. Later work waits for a frozen/accepted predecessor or the explicit external-block escape.
- **Acceptance boundary is fixed.** New non-gating improvements discovered after freeze go to later/backlog scope. They do not silently enlarge the current Section.
- **Reopen narrowly.** A DONE Section may reopen only for a demonstrated regression, invalid evidence, changed acceptance contract or breaking later integration; record the exact reason first.

Recommended lifecycle states are:
`OPEN -> IMPLEMENTING -> CANDIDATE_FROZEN -> QUALIFYING -> DONE`,
or `... -> INTERNAL_DONE_BLOCKED_EXTERNAL` when only an external unblock remains.
`REOPENED` is exceptional and must name the invalidated surface.

For the current front, durable state should identify: **canonical finisher**, **candidate SHA if frozen**, **remaining acceptance-critical gap**, and **exact unblock condition if externally blocked**.

## Rules

- Read the current canonical Section plan and live default branch before updating this file.
- Record only evidence-backed DONE state; never infer closure from a chat summary, a PR existing, queued CI, or a single green test.
- Once recorded DONE, a Section/Subsection is skipped by normal workers and is not re-entered unless it is explicitly marked REOPENED for a demonstrated regression, invalidated evidence, changed acceptance contract, or broken later integration.
- If parallel workers produce duplicate closure lineages, preserve one canonical lineage, converge unique required changes, then close/supersede the duplicate and delete the duplicate branch when safe.
- Update this ledger in the same run that closes or reopens scope.

## Closure registry

| Section / Subsection | State | Canonical evidence / lineage | Accepted source / build | Notes |
| --- | --- | --- | --- | --- |
| Section 0 — Жива технічна правда, конвергенція і reuse-first карта | DONE | Canonical plan rev `AHj4eMT7LJVKkY3WhLMh6OtcX1sQNy2lS5BZEACLatBusra17PnNcl8ZJOldiCS5vvGk0PvwOlDzy1V12aZ7KMw5QzseAK_J__d4wy5zOw`; live-main/active-lineage reconstruction under root `AGENTS.md`; canonical Section-2 aggregate #2227 demonstrates collision-aware reuse/convergence | `main@88fdd7c28b8bb72b5abdb9cc103ec4d22cd3e49f` | Historical closure backfill from exact current evidence; not counted as a new closure run. Current workers can select the earliest unfinished causal slice without duplicating canonical identity/runtime authorities. |
| Section 1 — Єдиний фінальний продуктовий контракт і Definition of Done | DONE | `docs/WHOLE_PRODUCT_COMPLETION_AUTHORITY.md@2036ebcc4216b99889f9cf7056c21c0eb35a18c7`; canonical plan rev above defines one ordered 46-Section acceptance matrix and final Section-45 whole-product gate | `main@88fdd7c28b8bb72b5abdb9cc103ec4d22cd3e49f` | Historical closure backfill from exact current evidence; not counted as a new closure run. V1/V2/stage labels cannot terminate product scope; final flags remain evidence-gated. |
| Section 2 — Канонічні доменні ідентичності та versioned schemas | DONE | canonical finisher PR `#2259`; exact qualified candidate `59182ba9c741b8048bd1e7f50d4d7ce34e02e9f0`; CI `37644908230` SUCCESS; Windows Candidate `37644908231` SUCCESS; Endurance `37644908250` SUCCESS; zero unresolved review threads; merge commit `c4c01262e506ae67ff1bd6936cda7ee7cca2db60` | qualified candidate `59182ba9c741b8048bd1e7f50d4d7ce34e02e9f0`, integrated into `main@c4c01262e506ae67ff1bd6936cda7ee7cca2db60` | **TERMINAL DONE.** Current plan acceptance was reread; ambiguity remains fail-closed, versioned/as-of identity survives restart through the qualified canonical implementation. Post-merge readback proves the merge commit has the exact qualified candidate as parent and adds no product-file delta beyond merge topology. Normal workers must skip Section 2 unless a concrete reopen condition is recorded. |
| Section 3 — Trusted chronology, causal time і decision cutoff | CANDIDATE_FROZEN / QUALIFYING | canonical finisher PR `#2238` / `gpt56sol/section3-prequal-wave-20261007`; exact repaired candidate `ab182a06ac07c3697fc8bdc88db199f71f12332f`; predecessor frozen candidate `34f9ca7c6f1d5a818d041120033182f5a2087713` was superseded only by the demonstrated endurance-cost repair `ab182a0`; plan revision `ANLCKQktbmDfdm6SqK2itrvxcxJfBTQXMJ9VSjxyLzoydMwFxtFIS5Xx743v_OlF2SXnSrA-sO-Sa8yFHfIyX6YpWAtVSWYtUfjtueXxnw` | candidate `ab182a06ac07c3697fc8bdc88db199f71f12332f`, based on product `main@c7d4601d08e1e2e35239fccabbb5b768ff689fba`; current-main drift remains control-only through this registry | **PRIMARY MUTATION FRONT — REFROZEN AFTER GATE REPAIR.** Repair bounds current-projection recovery to the authoritative stream tip while preserving semantic-tamper rejection and adds a direct bounded-cost regression. Zero unresolved review threads. Fresh exact-head qualification: CI `37656910673` IN_PROGRESS, Windows Candidate `37656910703` IN_PROGRESS, Endurance `37656910665` IN_PROGRESS at this readback. No further source mutation unless this exact candidate demonstrates another acceptance-relevant failure. DONE requires terminal SUCCESS on all three required gates, late-bind plan/main/topology/thread readback, integration with expected-head protection, post-merge readback and immutable closure record. |

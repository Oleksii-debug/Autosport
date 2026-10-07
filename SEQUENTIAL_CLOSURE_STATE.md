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
| Section 2 — Канонічні доменні ідентичності та versioned schemas | CANDIDATE_FROZEN / QUALIFYING | canonical finisher PR `#2227` / `gpt56sol/section2-integration-wave-20261007`; exact candidate `47288578d91b5f1ec6db1b7410ae2683fee4870e`; freeze checkpoint comment `#6038789228` | candidate `47288578d91b5f1ec6db1b7410ae2683fee4870e` against product base `main@fa170efa91e3cd51268b229f473792aeadd8927d` | **PRIMARY MUTATION FRONT — FROZEN.** Required exact-SHA gates: CI `37627397521`, Windows Candidate `37627397567`, Endurance `37627397555`; no candidate/source mutation unless one of these gates demonstrates an acceptance-relevant failure. Later control-only main movement does not invalidate product qualification unless it intersects Section-2 dependency surface. TERMINAL_DONE requires successful gates, integration, post-merge readback, then this row must be updated to DONE. |

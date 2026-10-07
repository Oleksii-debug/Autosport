# Autosport — immutable closed Sections

This file is a durable worker-control authority for normal autonomous work.

## Rule

A Section listed as `DONE_IMMUTABLE` MUST NOT be reimplemented, polished, re-audited, requalified, or otherwise mutated by ordinary workers.

The only permitted exception is an explicit `REOPENED` record created **before** new work and naming one concrete demonstrated reason:
- regression;
- invalidated closure evidence;
- materially changed acceptance contract;
- later integration that demonstrably broke the closed scope.

A worker discovering a non-gating improvement in a closed Section must place it in later/backlog scope rather than touching the closed Section.

## Locked Sections

| Section | State | Closure basis |
| --- | --- | --- |
| Section 0 — Жива технічна правда, конвергенція і reuse-first карта | DONE_IMMUTABLE | Canonical Section plan criteria unchanged through Drive revision 7; durable closure registry already records DONE; live reuse-first/closure authority exists in main. |
| Section 1 — Єдиний фінальний продуктовий контракт і Definition of Done | DONE_IMMUTABLE | Canonical Section plan criteria unchanged through Drive revision 7; `docs/WHOLE_PRODUCT_COMPLETION_AUTHORITY.md`, `docs/MASTER_TECHNICAL_PROJECT.md`, `docs/PRODUCT_VISION.md`, and closure registry bind one final Autosport product and its completion contract. |

Normal workers must skip Sections 0 and 1 and select the earliest unfinished Section after them.

Owner directive recorded 2026-10-07.


## Active frozen candidate

| Section | State | Exact candidate | Worker rule |
| --- | --- | --- | --- |
| Section 2 — Канонічні доменні ідентичності та versioned schemas | CANDIDATE_FROZEN_NO_TOUCH | `3870483b7810852b6f777f6c8b3d12c0e785d537` | Do not mutate the canonical finisher while exact-SHA CI / Windows Candidate / Endurance qualification is running. Only a demonstrated acceptance-relevant gate failure may authorize the smallest repair; otherwise preserve the exact SHA through integration. |

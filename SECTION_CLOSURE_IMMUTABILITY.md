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
| Section 2 — Канонічні доменні ідентичності та versioned schemas | DONE_IMMUTABLE | Exact candidate `59182ba9c741b8048bd1e7f50d4d7ce34e02e9f0` passed CI `37644908230`, Windows Candidate `37644908231`, and Endurance `37644908250`; PR #2259 merged with expected-head protection as product merge `c4c01262e506ae67ff1bd6936cda7ee7cca2db60`; post-merge readback confirmed integration. |

Normal workers must skip Sections 0, 1 and 2 and select the earliest unfinished Section after them.

Owner directive recorded 2026-10-07.


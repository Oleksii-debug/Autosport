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

## Plan 6 atomic terminal locks (canonical multi-plan register; 2026-10-21)

The atomic identifiers below are distinct from the legacy Section 0–2 identifiers above. They are terminal in `MULTI_PLAN_CLOSURE_STATE.md` and `ATOMIC_OPEN_SECTIONS_2026-10-13.md` on the inspected `main@d4dc137a4ca498f9848af1c4c492c4e4eca569bf`. This paragraph **does not** reopen or requalify an already completed feature, claim a new CI PASS, or declare Plan 6 itself complete.

| Plan 6 atomic Section | State | Existing accepted closure basis (no new implementation) |
| --- | --- | --- |
| 1А | DONE_IMMUTABLE | Ukrainian localization/source accessibility; PR #1303 and canonical Plan 6 §1 DONE. |
| 1Б | DONE_IMMUTABLE | Inherited terminal §1 critical localization/semantic CI contract. |
| 1В | DONE_IMMUTABLE | Inherited terminal §1 source keyboard/NVDA semantics; **not** physical NVDA attestation. |
| 2А | DONE_IMMUTABLE | Canonical WebView2 Windows application entry, controller, EdgeChromium shell and package build integrated through PR #1598, main merge `e86c2d1502d83e6ea61c2714c7c3a28c47873f0b`; [Plan 6 section 2 source-terminal receipt](docs/PLAN6_SECTION2_WEBVIEW2_SOURCE_TERMINAL_20261021.md). Physical Windows/NVDA final QA separate. |
| 2Б | DONE_IMMUTABLE | Integrated Ukrainian named semantic controls, keyboard-first PAPER surface, status/text alternatives; source/DOM readback [Plan 6 section 2 source-terminal receipt](docs/PLAN6_SECTION2_WEBVIEW2_SOURCE_TERMINAL_20261021.md); no false browser/NVDA PASS. |
| 2В | DONE_IMMUTABLE | Existing native emergency STOP on WebView2 failure and Python-controlled STOP action integrated through #1598; exact source readback [Plan 6 section 2 source-terminal receipt](docs/PLAN6_SECTION2_WEBVIEW2_SOURCE_TERMINAL_20261021.md). Owner NVDA is Plan 8. |
| 3А | DONE_IMMUTABLE | Final operator-UI source integration: 19/19 canonical Windows WebView2 actions/handlers, zero missing DOM controls/handler stubs, source-exact V8 15/15 + 18/18 scenario PASS; accepted main source `77e840a5f1e43eba489788f267f4dc00c641d367`, [terminal receipt](docs/PLAN6_SECTION3A_TERMINAL_SOURCE_20261021.md), PRs #2437/#2438. Final native Windows/physical NVDA in Plan 8 is NOT claimed. |
| 3Б | DONE_IMMUTABLE | Unsupported bookmaker surfaces visible-disabled with truthful reason; PR #2292; PAPER/SHADOW remains usable. |
| 3В | DONE_IMMUTABLE | Deterministic focus, recovery and actionable status; PR #2297. |
| 4А | DONE_IMMUTABLE | Canonical accessibility auditing, UIA/keyboard provenance and evidence export; PR #2289. |
| 4Б | DONE_IMMUTABLE | Completed source automation for labels, roles, focus, native UIA notifications/live regions, duplicate AutomationId/malformed-state and selectable reports; integrated source/readback and 20/20 static checks in [Plan6 4Б terminal evidence](docs/PLAN6_SECTION4B_MACHINE_ACCESSIBILITY_TERMINAL_20261021.md). Native Windows and physical NVDA are distinct final QA, not falsely claimed PASS. |
| 4В | DONE_IMMUTABLE | Machine audit does not falsely claim human/NVDA verification; existing qualified source, PR #2289. |
| 5А | DONE_IMMUTABLE | Existing Windows package/build/start/supply-chain source reuse; PR #2290. |
| 5В | DONE_IMMUTABLE | Fixture-only signing and update primitives; PR #2318; no production signed installer claimed. |
| 6Б | DONE_IMMUTABLE | Repository-controlled exact-source integrity and anti-downgrade guarantees; canonical package verifier, pinned Ed25519 preflight and same-schema SHA-anchored PAPER upgrade/rollback. [6Б terminal receipt](docs/PLAN6_SECTION6B_TERMINAL_20261021.md). Native full-checkout CI/Windows/NVDA and Plan 8 remain independent. |
| 6В | DONE_IMMUTABLE | Canonical private offline backup/fresh-root restore and Plan 5 lock coordination; PR #2325. |
| 7Б | DONE_IMMUTABLE | Existing canonical keyboard-only Ukrainian WebView2 PAPER controls and independently launchable Windows native emergency STOP are source-integrated from PR #1598; 14/14 inspected source/test blobs matched merged lineage at `main@7034cd6af4a1c4feb1cb3cd6ac81d7eba5b08b38`. [Terminal source evidence](docs/PLAN6_SECTION7B_KEYBOARD_STOP_SOURCE_TERMINAL_20261021.md). No claim of executed pytest/Windows/NVDA or Plan 6 aggregate closure. |

**Scope boundary:** Plan 6 §5Б, §6А, §7А and §7В and their unfinished aggregate parents are **not** marked DONE here. Plan 7 M1 and Plan 8 remain separate. Neither external real-money betting nor physical Windows/NVDA tests are represented as having been done. Normal agents must skip the terminal atomic sections above unless they first record a concrete `REOPENED` reason under this file's rule. The exact integration/test evidence for each accepted closure remains in the canonical multi-plan register and linked evidence files, not redefined by this skip-list.

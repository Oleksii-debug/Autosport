# Autosport — План 6, розділ 4А — TERMINAL reuse receipt (2026-10-21)

**РОЗДІЛ №4А / План 6 — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Fixed approved acceptance boundary
This *atomic* section only requires reusing the already integrated canonical accessibility audit, event-announcement policy, keyboard audit, UIA provenance evidence and actual evidence export. It does **not** claim to close the independent 4Б (WebView2 semantic automation), 4В (machine/physical evidence segregation), 2А–2В (new semantic shell), Plan 7, Plan 8 or full physical NVDA acceptance. Approved source: `ATOMIC_OPEN_SECTIONS_2026-10-13.md`, original [Plan 6](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit).

## Reuse and real product integration
The existing `src/autosport/accessibility_audit.py` uses the *actual* `WindowsAutosportApp` UI and `tk_uia.describe` provider evidence, checks actual widget state, projects audit failures and saves bounded JSON; `keyboard_audit.py` runs the canonical operator focus, shortcut and Tab-reachability source checks. `accessibility_announcements.py` is the canonical SILENT/POLITE/ASSERTIVE policy, not a second runtime. `gui_evidence_export.py` projects product evidence export through the existing worker, and `evidence_export.py` validates/export-binds source evidence and its SHA-256 manifest. The external `scripts/external_uia_audit.ps1` supports actual Windows UIA machine provenance, not NVDA speech. The linked test suites exercise these implementations.

This is a **source-reuse closure**, not a new parallel module. Existing canonical implementation is on accepted `main`; no feature-only branch or standalone inaccessible implementation is needed for section 4А. The historic Tk UIA evidence is not represented as acceptance of the pending WebView2 replacement.

## Exact blob provenance
Independent GitHub `fetch_file` readback compared `main@86358408f2e5c630be4c514991aa167722a861da` and exact CI-qualified PR #2289 source head `1ed86090b04dd5f3fcc6bfd83b63b791ed992c25`. All **13/13** scope-relevant production/script/test paths were byte-identical:

| Scope path | Exact Git blob SHA (both refs) |
| --- | --- |
| `src/autosport/accessibility_audit.py` | `44e20ac716a3c68d182b56627e469010227b0b6a` |
| `src/autosport/accessibility_announcements.py` | `21621bca6fbe29d0999b3a1286bf2f164f414808` |
| `src/autosport/keyboard_audit.py` | `6970481e7ff924f63942c18616bb04e0694e5859` |
| `src/autosport/evidence_export.py` | `dfaeaf8cf41ea487d6680c3b1950ca06bd28322e` |
| `src/autosport/gui_evidence_export.py` | `68d04908e4bc780b8fdf082bfbee9a6f8293be44` |
| `scripts/external_uia_audit.ps1` | `dbf0eaa157a9c07f31bd0274ac79c58e7f78923b` |
| `tests/test_accessibility_audit.py` | `6cbe10ac54dcd62943992262437cc72f1e597658` |
| `tests/test_accessibility_announcements.py` | `e860719eca736c1e01618a01c9321db3c1421176` |
| `tests/test_keyboard_audit.py` | `567445ce9863579154b1e5413f0c5cb44282c0e9` |
| `tests/test_gui_evidence_export.py` | `2faedf1996251b5c4f49e6a372b92a5b726efc19` |
| `tests/test_external_uia_audit_contract.py` | `50db66ab200bb7dc1ce14f2cfc89e59ad1f007b7` |
| `tests/test_evidence_export.py` | `af5eb22fcef2dff6517e36c0c2815779b21eb691` |
| `tests/test_nvda_acceptance_evidence.py` | `c73576cafca096fa770b72db3a8feb1869070355` |

PR [#2289](https://github.com/Oleksii-debug/Autosport/pull/2289) merged to `main` as `8754275c6653022e417478880d52591ea8cf6dfe`. On the exact PR head above, [CI #39542347381](https://github.com/Oleksii-debug/Autosport/actions/runs/39542347381) and [Windows Candidate #39542347333](https://github.com/Oleksii-debug/Autosport/actions/runs/39542347333) both returned `completed/success` on 2026-10-21. This proves the *same source and tests* qualified and subsequently integrated; it does not claim tests newly executed on this docs-only closure SHA. Recent #2289 repairs also retained fail-closed redacted export diagnostics instead of raw Python exception leakage.

## Terminal disposition and boundaries
`remaining_approved_implementation_for_Plan6_4A=0`. No source rewrite or new test files are made for this atomic section. Runtime consumer integration, existing test coverage and exact blob readback are present in main. Broader Section 4 remains open because 4Б and 4В are independent; Section 2 WebView2 PR #1598 remains unmerged and has its own pending exact-head machine gates.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

**Do not reopen or reimplement Plan 6 §4А without an explicit new requirement or a concrete reproduced regression.**

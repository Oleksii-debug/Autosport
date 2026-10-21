# Autosport — Plan 6 §4В: machine accessibility evidence truth boundary (2026-10-21)

**РОЗДІЛ №4В (План 6) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Canonical acceptance boundary

“Automated evidence never falsely claims physical NVDA verification” is the entire approved scope of this **independent atomic section**, per `ATOMIC_OPEN_SECTIONS_2026-10-13.md` and [Plan 6](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit).

The canonical production `autosport.accessibility_audit` and `autosport.keyboard_audit` emit `human_tested=false`, `nvda_verified=false`, and `real_money_execution=false` even when their own machine probe reports PASS. Their failure branches also emit the same false fields; exceptions are redacted into structural categories so exported machine evidence cannot reveal private credentials. The separate `nvda_acceptance` module maintains a **physical-evidence workflow** with explicit owner/tester checks, manifest evidence and SHA-bound package, and does not upgrade a machine report into a physical-human verdict. Both the Windows packaged NVDA artifact contract and machine accessibility tests explicitly require false human/NVDA truth labels. This subsection does not waive final manual acceptance — it prevents accidental *misrepresentation*.

The implemented result is wired into the real existing Windows audit/export consumers; no new surrogate audit, unconnected file, bookmaker credentials, or real-money instructions were created. 4А and 4Б concern broader audit composition/coverage and remain independent incomplete engineering sections. Replacement WebView2 §2А–§2В and its unmerged PR #1598 remain independently responsible for preserving this truth contract after integration.

## Qualified main evidence

Frozen source head `1ed86090b04dd5f3fcc6bfd83b63b791ed992c25` of merged PR #2289 passed exact-head [CI 39542347381](https://github.com/Oleksii-debug/Autosport/actions/runs/39542347381) and [Windows Candidate 39542347333](https://github.com/Oleksii-debug/Autosport/actions/runs/39542347333): both verified `completed/success`, not skipped controller jobs. That tested head was integrated into main via merge `8754275c6653022e417478880d52591ea8cf6dfe`.

All **11/11** directly relevant production/test Git blob SHAs were re-read and found identical between exact qualified head and integrated current main (pre-closure `48d3d3ea68da0ef5b3f5c14eb87f846e9f6602a9`):

| Repo path | Git blob SHA common to frozen head/current main |
|---|---|
| `src/autosport/accessibility_audit.py` | `44e20ac716a3c68d182b56627e469010227b0b6a` |
| `src/autosport/keyboard_audit.py` | `6970481e7ff924f63942c18616bb04e0694e5859` |
| `src/autosport/nvda_acceptance.py` | `98d132dafb88a1975a41dae70111246edc217b8d` |
| `tests/test_accessibility_audit.py` | `6cbe10ac54dcd62943992262437cc72f1e597658` |
| `tests/test_keyboard_audit.py` | `567445ce9863579154b1e5413f0c5cb44282c0e9` |
| `tests/test_external_uia_audit_contract.py` | `50db66ab200bb7dc1ce14f2cfc89e59ad1f007b7` |
| `tests/test_packaged_audit_exception_diagnostics.py` | `b76008abd87931bcf00e66d68ae051e641e7d0f6` |
| `tests/test_nvda_acceptance_evidence.py` | `c73576cafca096fa770b72db3a8feb1869070355` |
| `tests/test_nvda_acceptance_json_integrity.py` | `9c187d98c02408fec3e18b4f20539e0b5795ce28` |
| `tests/test_windows_nvda_artifact_contract.py` | `2e1421e154afbc6cafada5111fb1cbc35a4bf584` |
| `tests/test_windows_shell_accessibility_runtime.py` | `1490416721c0d38a76f43cdb42dea00400e143fc` |

No source code or test was altered by this closure. It is not a new current-main CI PASS; actual device/owner/NVDA verification remains **NOT_VERIFIED** until Plan 8 final release stage. No complete local test execution was claimed because GitHub network resolution to a checkout failed in the local container.

`remaining_approved_implementation_for_Plan6_4В=0`.
`REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.

**Parent Plan6 Section 4, sibling 4А/4Б, #1598 and Plan 8 are NOT closed here.** Reopen only on concrete demonstrated misrepresentation or newly approved scope.

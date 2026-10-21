# Autosport — Plan 6 / atomic Section 4В — terminal source truth (2026-10-21)

**РОЗДІЛ №4В (План 6) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

Scope, from the canonical Plan 6 and `ATOMIC_OPEN_SECTIONS_2026-10-13.md`: **automated accessibility/NVDA evidence must never falsely claim physical NVDA verification**. This is an independently closable engineering truth-label contract; it does NOT close 4А, 4Б, parent 4, WebView2, final physical NVDA acceptance, or whole-product release.

## Reuse of accepted implementation already in main

No replacement accessibility subsystem and no source changes are necessary for this atomic truth-label obligation:

- `src/autosport/nvda_acceptance.py`: template preserves `human_tested=false`, `nvda_verified=false`, `v1_ready=false`; validator rejects claims that promote these labels and independently emits `machine_verified_physical_execution=false` and `requires_owner_release_decision=true`, **even if a supplied human record describes PASS**.
- `scripts/nvda_evidence_package_smoke.ps1`: packaged and freshly extracted template evidence both remain explicitly `human_tested=false`, `nvda_verified=false`, `machine_verified_physical_execution=false`; a machine-only smoke cannot be physical acceptance.
- `src/autosport/accessibility_audit.py`: both PASS and failure publication fix `human_tested=false` and `nvda_verified=false`; current-main diagnostic redaction is stricter than the earlier qualified version and does not change this false-claim invariant.
- `src/autosport/windows_lab_contract.py` and the existing source-level tests preserve the distinction between machine qualification and physical NVDA acceptance.

## Exact Git blob and qualification evidence

The following six required files have **byte-identical blob SHAs** on source-integrated `main@48d3d3ea68da0ef5b3f5c14eb87f846e9f6602a9` and qualified source head `d93d203e867c71aad880f1b2e3d144549e166030`:

| Path | Exact SHA |
| --- | --- |
| `src/autosport/nvda_acceptance.py` | `98d132dafb88a1975a41dae70111246edc217b8d` |
| `tests/test_nvda_acceptance_evidence.py` | `c73576cafca096fa770b72db3a8feb1869070355` |
| `tests/test_windows_nvda_artifact_contract.py` | `2e1421e154afbc6cafada5111fb1cbc35a4bf584` |
| `scripts/nvda_evidence_package_smoke.ps1` | `d3010832f91186773411253cd276e33ba353994f` |
| `src/autosport/windows_lab_contract.py` | `dec18dea418ef20e021cbaaa2fa271d522a51143` |
| `tests/test_windows_lab_contract.py` | `cd044c7726fa1b7dea604211af5e58bc1b065cc5` |

This exact qualified source head completed [CI 38148320018](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018) **SUCCESS** and [Windows Candidate 38148320047](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047) **SUCCESS**. The six blob equalities were checked live before this closure; older test PASS is correctly scoped to the unchanged source identity rather than misrepresented as current-main CI.

Two related current-main files intentionally differ from that older qualified head because a separate 2026-10-21 security fix redacted private exception text: `src/autosport/accessibility_audit.py` (`44e20ac716a3c68d182b56627e469010227b0b6a`) and `tests/test_accessibility_audit.py` (`6cbe10ac54dcd62943992262437cc72f1e597658`). Their `human_tested=false`/`nvda_verified=false` output remains visibly enforced in both PASS/failure branches; **no newer exact-head full-suite PASS is claimed for those two files**. This evidence describes independent `4В` false-promotion truth, not full parent-4 audit completeness.

`tests/test_nvda_acceptance_evidence.py` explicitly checks rejection/retention of false labels, pending template semantics, and successful human-record format validation without machine promotion. `tests/test_windows_nvda_artifact_contract.py` checks the packaged smoke's explicit false flags.

## Boundary and terminal decision

`remaining_approved_implementation_for_Plan6_4V=0` for the **narrow original approved Section 4В only**. Main already contains the real machine evidence and false-claim guards; no unmerged 4В implementation dependency was found. Do not reopen for additional polish without a new approved requirement or a concrete regression.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

Pending/unrelated: 4А/4Б accessibility coverage, WebView2 #1598, 5Б/5В packaging, Plan 7 convergence, Plan 8 physical NVDA and external real bookmaker acceptance. This acceptance is **not** a claim of final package or owner testing.

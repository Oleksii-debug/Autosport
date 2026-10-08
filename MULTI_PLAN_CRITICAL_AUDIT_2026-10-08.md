# Autosport — Multi-Plan Critical Audit — 2026-10-08

## Result
- New plans: 8.
- New Sections: 67.
- Numbered Subsections: 201.
- New Section 0 count: 0.
- Drive folder/file naming normalized: `Проєктні плани`; `1. Перший план` … `8. Восьмий план`.
- Legacy coverage: 46/46, unmapped 0.

## Dependency model
Plans 1–6 are independent engineering plans and may finish in any order.
Plan 4 bookmaker/provider/execution engineering is offline/fixture-capable and requires no real bookmaker credentials for component DONE.
Plan 7 is provider-free PAPER/SHADOW convergence and waits on terminal Plans 1–6. Plan 4 remains parallel/offline-actionable, but its provider/public-browser qualification is required for the final end-to-end convergence.
Plan 8 is dependency-aware external/real-execution/NVDA/final release.

## Migrated truth
- legacy 0–1 remain accepted control/product baselines;
- legacy 2 -> Plan 1 / Section 1 DONE;
- legacy 3 -> Plan 1 / Section 2 QUALIFYING; reuse canonical PR #2238/frozen lineage;
- legacy 4 -> Plan 1 / Section 3 PARTIAL_EXISTING; reuse PR #2261;
- later rich source/test work is PARTIAL_EXISTING rather than greenfield.

## Parallelism rule
A plan may close against stable peer contracts/fixtures. Fixture/source evidence never masquerades as PAPER/real/physical evidence. Major integration defects return to the owning engineering plan.

## External boundary
No worker may block Plans 1–7 merely because a bookmaker/account/credential is absent. No CAPTCHA, geo, authentication-security or anti-bot bypass. Real account and money-moving evidence belongs only to Plan 8.

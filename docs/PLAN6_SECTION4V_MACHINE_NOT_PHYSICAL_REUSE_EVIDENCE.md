# Autosport — Plan 6, Section 4В: machine evidence is not physical NVDA acceptance

Scope: automated evidence must never promote itself to proof of physical NVDA testing. Section 4В is independent from 4А/4Б and from final Plan 8 owner testing.

## Existing integrated implementation

- `src/autosport/nvda_acceptance.py` preserves false `human_tested` and `nvda_verified` in templates and machine validation. Even a fully PASS human-supplied checklist does not let software certify physical execution; source/package anchor equality does not prove provenance.
- `src/autosport/release_package.py` sets and verifies false physical-attestation flags in the package.
- `scripts/nvda_evidence_package_smoke.ps1` uses false machine-physical-claim flags in its Windows artifact.
- `tests/test_nvda_acceptance_evidence.py`, `tests/test_nvda_acceptance_json_integrity.py`, `tests/test_windows_nvda_artifact_contract.py` reject false promotion, malformed evidence, and false source/package anchors.
- `.github/workflows/windows-build.yml` wires machine checks to actual artifact evidence. `docs/ACCESSIBILITY_TK_UIA_PROVENANCE.md` separates machine UIA from human NVDA.

## Existing exact-source qualification

Eight implementation/test/workflow/doc paths above match byte-for-byte by Git blob SHA between integrated `main@48d3d3ea68da0ef5b3f5c14eb87f846e9f6602a9` and the qualified head `d93d203e867c71aad880f1b2e3d144549e166030`. [CI run 38148320018](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018) and [Windows Candidate 38148320047](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047) both completed SUCCESS at exactly that qualified source head. The reuse evidence is not a new current-main or physical-test run.

There is no missing repository-controllable implementation specifically for Section 4В. Source changes are unnecessary. Plan 6 Sections 2А–2В, 4А–4Б, 5Б–5В, final Windows/NVDA acceptance, and whole-product integration remain outside this narrow closure.

No physical NVDA test was performed; `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.

**Terminal entry becomes effective only after this record is integrated into main and the canonical registry confirms readback.**

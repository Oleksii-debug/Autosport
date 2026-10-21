# Plan 6 — РОЗДІЛ №6Б — ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL

Date: 2026-10-21. Scope: the approved atomic **6Б** requirement only: *package/update never bypasses integrity policy or silently downgrades schema/state*. This is **not** a declaration that the separate **6А** migration/installation transaction, **5Б** full bundled SBOM/licence evidence, Section 2 WebView2 shell, Plan 7, Plan 8, or the whole product is finished.

## Accepted source and integration

The existing production implementations were already merged into canonical `main` at the source-evidence baseline `ca13e2fefbbd6d232bed48ef7761679c6d15e39b`. No duplicate verifier, updater or packaging implementation is created by this terminal record.

| Canonical path | Exact Git blob at source baseline | 6Б behavior |
|---|---|---|
| `src/autosport/release_package.py` | `15f3f7e75516b644dc45a070a50b22eb97cbb6e0` | Independent ZIP/source/manifest/checksum verification and forbidden private-state member checks |
| `src/autosport/windows_update_admission.py` | `27b4bafbf6728eadc32440c737cdbbbaddd2a4bb` | Strict version/schema monotonicity; same-version repackage and incomplete migration chain refusal; preflight never authorizes install |
| `src/autosport/windows_update_signature.py` | `6869056f1b364a238fab9d1e99be3002f3e671d0` | Ed25519 source/package/version/schema claim; invalid signature/digest fail closed |
| `src/autosport/windows_update_installed_trust.py` | `68c514c72198a955c73b14cc998bf337d420d8ec` | Pinned installed-source/public-key trust; preflight-only boundary |
| `src/autosport/windows_update_preflight.py` | `163cdacad639d0759f2015f2f410da95a1174c31` | Signature/payload/snapshot admission, late authority-rebind checks and non-authorizing disposition |
| `src/autosport/offline_release_upgrade.py` | `533bab54901f266d9904216ce80009c30f4be64e` | Independently SHA-pinned PAPER upgrade/rollback; refuses downgrade, no-op, schema-changing transaction; preserves verified previous release |
| `src/autosport/cli.py` | `5110f75aba618e6d9e32122ea1a06166b50c1d4c` | Existing install/upgrade/rollback/recovery command routing |
| `src/autosport/data_tools_entry.py` | `1a36802f15cbb1aa3788c0d392dc6b26e58b60c2` | Packaged `Autosport-Data.exe` update/signature diagnostic and offline PAPER lifecycle routing |

The actual shipping update and installation path is not replaced. Signatures and the signed diagnostic remain **non-authorizing preflight**; the separately pinned, same-schema PAPER transaction is limited to its advertised capabilities and never turns on real bookmaker execution. A schema-changing data migration is explicitly refused rather than silently performed; implementing it is a separate open **6А** responsibility.

## Verification class — truthful boundary

- **GitHub source readback:** PASS for the eight exact blobs above on the immutable source baseline; the source-only checks examined the canonical downgrade and migration guards, independent package verification, pinned installed trust, CLI/Data.exe routing, and the no-replace PAPER upgrade/rollback controls.
- **Existing regression source verified present:** `tests/test_windows_update_admission.py@18a6d4fe1f45df41bb44449cf0d71f60c1dc3c00`; `tests/test_windows_update_signature.py@83cbcf48781bd237637db98c41acab1c47d199c9`; `tests/test_windows_update_preflight.py@155130499f431d518182b9886a4162f4cb6da4b1`; `tests/test_windows_update_installed_trust.py@3682eb63dc638486fe43bc5aec595705b27a7d33`; `tests/test_offline_release_upgrade.py@081a3ab65606399017bc51a204f6f50a5420bc3d`. They include negative/identity/late-rebind/migration/downgrade/rollback/crash recovery cases. These are test-source evidence, **not** a fabricated execution count.
- **Native complete-checkout Python tests:** NOT_EXECUTED here; this worker's container cannot DNS-resolve github.com to clone the complete checkout. Available GitHub Actions jobs on the recently updated project remain QUEUED/SKIPPED rather than recorded green. This does not claim a live tested Windows binary.
- **Signed production release, installed exact Windows package, human NVDA, external bookmaker execution, real-money execution:** NOT_VERIFIED / NOT_AUTHORIZED. Keep the final Plan 8 gates independent.

## Terminal instruction

The accepted *repository-controlled, fail-closed integrity / anti-downgrade policy* for atomic **Plan 6 Section 6Б** has no further identified approved implementation task. **РОЗДІЛ №6Б — ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL; remaining_approved_implementation_6B=0.** Do not reassign the same original requirement or reopen it for general release/NVDA/SBOM checks. A newly demonstrated violation on a concrete canonical path can be a separately scoped regression repair with truthful evidence, not an automatic reopening of accepted original feature development.

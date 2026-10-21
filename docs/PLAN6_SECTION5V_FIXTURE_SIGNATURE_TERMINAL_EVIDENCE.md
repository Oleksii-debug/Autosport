# Autosport Plan 6 §5В — terminal source development readback (2026-10-21)

**РОЗДІЛ ПЛАНУ 6 №5В — ЗАКРИТО ДЛЯ ПОДАЛЬШОЇ РОЗРОБКИ / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Accepted scope
Fixture material is used to implement signing/update primitives. Production signer keys, final delivered signature and customer NVDA acceptance belong to Plan 8; none are claimed here.

## Integrated implementation
- `scripts/qualify_fixture_release_signature.ps1`: 3072-bit ephemeral RSA key, two-day self-signed X.509 fixture cert; exact UTF-8 source-SHA and release-ZIP SHA-256 manifest, public-certificate signature verification, tampered source/digest/signature negative checks, trusted pin sent to independent verifier, no persisted private key.
- `scripts/verify_release_update_signature.ps1`: source/archive hash, caller-pinned public X.509 certificate, certificate validity, RSA signature, rehash-before-positive update admission, no owner/real-money authority.
- `.github/workflows/windows-build.yml`: fixture signature qualification and artifact evidence step after package/SHA generation; final production signed artifact not implied.

## Exact GitHub state
- Canonical PR https://github.com/Oleksii-debug/Autosport/pull/2318 merged `main@f79310a9702133ff78d3f47e9a661f4428737158`; PR head `69955ac5ba5afb577734dd508ac060981dad33b0`.
- Postmerge main Git blobs equal frozen head: `.github/workflows/windows-build.yml` `32049b5229bedd883cce4bfa4e85ba0d3428cf03`; `scripts/qualify_fixture_release_signature.ps1` `cecd9acfe2db35b2e2e76f5f8cb3e77495def856`; `scripts/verify_release_update_signature.ps1` `c5b3fc7ce9e4b3f453ceb854034147e9a0940b65`.

## Test and evidence class truth
The actual exact-head CI and Windows workflow executions were **QUEUED/NOT EXECUTED** at readback; superseded-control runs completed SKIPPED. A commit/workflow carrying a test is not proof that it ran. This is terminal *source-development* acceptance under the owner's no separate intermediate test-stage directive, not completed artifact/Windows execution evidence. When Windows infrastructure runs, any concrete failing check may reopen the narrow defective implementation.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_SIGNED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

# Autosport — План 6 / розділ 5А — остаточне закриття розробки (2026-10-21)

**РОЗДІЛ №5А (План 6) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**
**Тільки 5А**, не батьківський розділ 5: незалежні 5Б і 5В лишаються незавершеними.

## Затверджений scope

Повторно використати наявні `release_package`, Windows entry/start guide/build scripts і supply-chain metadata для єдиного Autosport Windows candidate. Це **reuse/qualification**, а не нова реалізація паралельного пакувальника. Перелік походить із канонічного `ATOMIC_OPEN_SECTIONS_2026-10-13.md` та [Плану 6](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit).

## Справжня інтеграція в main — звірка blob SHA

Source-integrated main at readback: `64542242c4002693f4310bcf25c78966444d4228`.
Historical qualified source head: `d93d203e867c71aad880f1b2e3d144549e166030`, merged as PR #2261 into the existing `main` lineage. All **16/16** scope-relevant source, script, configuration, documentation and focused test blobs are identical on qualified head and on the readback main, without backport, source rewrite or unmerged dependency for **5А**:

| Canonical file | Exact common Git blob SHA |
|---|---|
| `src/autosport/release_package.py` | `1a057c8f098c0e46a9df525e29b4719570ae5797` |
| `src/autosport/windows_entry.py` | `f6510b66c17c27358f0fbaf0c81c7832bcd9f3e9` |
| `scripts/build_windows.ps1` | `790c21b4972cfb6094479e5b72f83c43b4161e17` |
| `scripts/build_windows_candidate.ps1` | `5342ab4b77e1ffab7c35a9f77352e031d5cef456` |
| `WINDOWS_START_HERE.txt` | `65515ec10a197b2437d8cd927223b36d9ba564a6` |
| `scripts/package_windows.py` | `7137f7ce2d4b29ed1ee5218d488cb0f32de38c5b` |
| `pyproject.toml` | `3d87772c4f40bf2d4a654be10866f1ed47b8334d` |
| `tests/test_packaging_entrypoints.py` | `2f9ce063c055236797c83625bdc2f6c513d04ad4` |
| `tests/test_windows_start_guide.py` | `1c19cf1708ff8332bf25936228eb0e7d125f6442` |
| `tests/test_release_package_source_identity.py` | `c4c055506115145ba14d4f04c7497a45be979a9d` |
| `tests/test_release_package_manifest_schema_integrity.py` | `7928aea0fc80024c791ad740019ef78d9398215a` |
| `tests/test_windows_candidate_critical_path.py` | `bf1c1605cc2f566c70cb243029e711013c2fbaa4` |
| `tests/test_windows_build_source_preflight.py` | `238d0051942702d431198c48d38b0b94e88f40c2` |
| `tests/test_package_windows_secret_exclusion.py` | `11a5c05ac2ef8090194b7089debd2f1a696d39fe` |
| `tests/test_windows_release_workflow_supply_chain.py` | `46eb28d58ac185ef834c26d42cb623681b0b97f5` |
| `tests/test_windows_exact_packaging_environment.py` | `47231ad569e644e0457c581315293a6ba2254d2d` |

The existing `.github/workflows/windows-build.yml` is also byte-identical: `0ce9d92dbbdc2d883c2e1561f726339ccf36928d`, yielding **17/17 total** including the runner wiring.

## Кваліфікація / жодних вигаданих PASS

The frozen exact-head [CI run #38148320018](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018) and [Windows Candidate #38148320047](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047) both report `completed/success` on exact `d93d203e867c71aad880f1b2e3d144549e166030` (verified again on 2026-10-21). This is **reused qualified source/test identity**; it is not a claim that fresh current-main CI finished or that a local Windows host was exercised during this closure. This docs-only closure adds no production code and no new test changes. In this executor, repository checkout failed because GitHub DNS was unavailable; no local complete-suite PASS is claimed.

Source scope accepted here is **reuse of the baseline package infrastructure only**. Work to add stage-neutral final package identity, deterministic final package material, signer/update primitives or new full WebView2 shell remains independently tracked by **5Б, 5В, 2А–2В** (including PR #1606 and #1598); these are not required to pretend this existing baseline reuse is new work. Plan 7 convergence and Plan 8 external/NVDA/real-money remain separate.

`remaining_approved_implementation_for_Plan6_5A=0`.
`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

**No reimplementation of Plan 6 Section 5А without a new owner requirement or concrete reproduced regression.**

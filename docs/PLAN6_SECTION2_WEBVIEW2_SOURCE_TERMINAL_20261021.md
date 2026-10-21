# Autosport — План 6, розділи 2А, 2Б, 2В — РОЗРОБКУ ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL

Date: 2026-10-21. Source integration scope only. Do not confuse with delivered Windows installer, physical owner/NVDA acceptance, or real-money authorization.

## Canonical PR integrated

- Original WebView2 source owner [#1598](https://github.com/Oleksii-debug/Autosport/pull/1598) merged with expected exact head `deb0a8f2a30f505bbd87652aa024bebfa1f76377` into `main@e86c2d1502d83e6ea61c2714c7c3a28c47873f0b`. GitHub response `merged=true`.
- Before merge: `mergeable=true`, `draft=false`, no unresolved review threads, compare `ahead=264, behind=0`. No alternative shell/finisher introduced.
- 13 exact Git blob readbacks **identical** between merged head and post-merge main:
  - `src/autosport/windows_entry.py@20c0b6a993c4b1170c0200e0ad388d9452b240f1`
  - `src/autosport/windows_web/index.html@33b3d6758e5268bda091949e5029a7041bd1c3b2`
  - `src/autosport/windows_web/app.js@26dc9d7b8a4eab79a8e7f6a70b2343151be16cb9`
  - `src/autosport/windows_web/emergency_stop.js@0a75c08af88a77713acdc493b4ccde2cb169ba5d`
  - `src/autosport/windows_webview_shell.py@4d7b875f250c49a67a0b764a38888fb7258e741b`
  - `src/autosport/windows_webview_emergency_stop.py@f571e39ab48516d1dc130b2bbde94e1eee153c59`
  - `src/autosport/windows_emergency_stop.py@7430843791b452864548da25d1c3acaf8630b8a3`
  - `scripts/build_windows.ps1@4f113e00d70973b2a0b8d889334305d7974b32a6`
  - `pyproject.toml@0127c7efc944a06cfca936e86fd27fd177e89f56`
  - `tests/test_webview2_bridge_session_origin.py@ab6c0ffdbbd23daf686c04f6ac8fe72c4e5b1739`
  - `tests/test_webview2_runtime_renderer_witness.py@332f8b6bcd2cc50547a0b5993d27c2f80b1353f7`
  - `src/autosport/windows_webview_audit.py@8180d05a252327ef9cd2c44bc3a6ff96218e16c1`
  - `.github/workflows/windows-build.yml@d4d7e82096216aecf4d646732381912e13dfc150`

## Individually accepted source scope

- **2А — CLOSED_FOR_DEVELOPMENT / TERMINAL.** Reused the established Autosport services, application controller, canonical Windows entry, WebView2 renderer and packaged build path. No parallel domain runtime.
- **2Б — CLOSED_FOR_DEVELOPMENT / TERMINAL.** Semantic HTML headings/landmarks, form controls and Ukrainian explicit accessible labels, PAPER/runtime and evidence flows, accessible text/status, keyboard-first DOM and source-level accessibility testing integrated in canonical shell.
- **2В — CLOSED_FOR_DEVELOPMENT / TERMINAL.** Canonical Python native emergency STOP `--native-emergency-stop` and Windows fallback stay available when WebView/browser startup fails; on-page STOP delegates through canonical controller, not JS-owned financial permission.

## Evidence class and outstanding independent obligations

- Read-only in-memory HTML/JS static checks against exact merged head: 113 unique DOM IDs; **0 duplicates**; **51 input/select/textarea controls and 0 without an explicit HTML label or aria-label**; 63 distinct static `byId(...)` references and STOP selectors all resolve to existing DOM IDs. This is a static check, **not** browser/keyboard execution.
- Regression test source is merged and byte-identical; complete native `pytest`, physical Windows packaged UIA, live browser and physical NVDA **NOT_EXECUTED / NOT_VERIFIED** in this closure. Latest exact-head CI `39664468989`, Windows candidate `39664468887`, Endurance `39664469144` were **QUEUED**, not PASS. Earlier failing old-head accessible-name assertions led to the integrated 30-name fix from child #2285 and related #2286; do not misrepresent old-head checks as new-head PASS.
- Plan 6 `3А`, `5Б`, `6А`, `7А–7В`, Plan 7, and Plan 8 remain OPEN or WAITING by their individual scope. Original issue #1506 retains the physical Windows+NVDA final-product acceptance requirement; neither source closure nor a GitHub merge pretends to satisfy that owner oracle.
- `real_money_execution=false`, `human_tested=false`, `nvda_verified=false`, `whole_product_complete=false`.

**Decision:** accepted Plan 6 source implementation of independent Sections 2А, 2Б and 2В is now present in one canonical `main`; `remaining_approved_source_implementation_for_each=0`. No duplicate feature development without new approved scope, proven regression, or invalidated evidence.
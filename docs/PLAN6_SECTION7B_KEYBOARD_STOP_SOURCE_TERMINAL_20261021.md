# План 6 / розділ 7Б — terminal source-development evidence (2026-10-21)

**РОЗДІЛ № 7Б — ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL** for the *approved source-level keyboard-only workflow and native emergency fallback* acceptance boundary. This is not a claim that Plan 6 Section 7 (aggregate), Plan 7 convergence, Windows installation acceptance, physical NVDA, or real-money wagering is complete.

## Accepted integration identity

- Baseline source: `main@7034cd6af4a1c4feb1cb3cd6ac81d7eba5b08b38`, including canonical WebView2 shell merge [PR #1598](https://github.com/Oleksii-debug/Autosport/pull/1598) at `e86c2d1502d83e6ea61c2714c7c3a28c47873f0b`.
- For 14 checked source and test paths, the GitHub blob SHA in that integrated main was byte-identical to the accepted PR #1598 head `deb0a8f2a30f505bbd87652aa024bebfa1f76377`; no competing GUI/backend/STOP implementation was written.
- Canonical Windows launcher: `src/autosport/windows_entry.py@20c0b6a993c4b1170c0200e0ad388d9452b240f1`.
- Canonical Python WebView controller and dispatch: `src/autosport/windows_webview_shell.py@4d7b875f250c49a67a0b764a38888fb7258e741b`.
- Browser UI: `src/autosport/windows_web/index.html@33b3d6758e5268bda091949e5029a7041bd1c3b2`, `src/autosport/windows_web/app.js@26dc9d7b8a4eab79a8e7f6a70b2343151be16cb9`, `src/autosport/windows_web/emergency_stop.js@0a75c08af88a77713acdc493b4ccde2cb169ba5d`.
- Native independent STOP and bridge: `src/autosport/windows_emergency_stop.py@7430843791b452864548da25d1c3acaf8630b8a3`, `src/autosport/windows_webview_emergency_stop.py@f571e39ab48516d1dc130b2bbde94e1eee153c59`.

## Fixed-scope evidence

1. The actual packaged WebView document is Ukrainian (`lang="uk-UA"`) with a working skip-link, main landmark, named sections and semantic native controls; its status is an ARIA live region. Static source inspection found **71** form/button controls with **zero unlabeled input/select/textarea controls** and **zero duplicate IDs**; the relevant dataset, strategy, research, replay, recovery, export, PAPER runtime, economic risk and STOP controls all exist. This is a source audit, **not a launched Windows test**.
2. The actual frontend dispatch uses `globalThis.pywebview.api.dispatch`; the canonical controller maps `dataset.select`, `strategy.set`, `research_plan.select`, `replay.run`, `recovery.run`, `evidence.export`, `product_runtime.start/stop`, `owner.preview/initialize`, and other PAPER actions to production handlers. Browser polling, focus handoff and unknown-result acknowledgement are implemented rather than no-op buttons.
3. Documented `F2` navigation and `F8` evaluation use the same real focus/navigation model; the UI retains native Tab controls and explicitly avoids stealing browser/assistive-technology shortcuts. The source-level focus/mapping tests exist in `tests/test_windows_webview_initial_focus.py@186b1d90e6796570d328ce74fa4f81d734fde932`, `tests/test_windows_webview_surface_target_focusability.py@892cee6821810b721d097614869f6369caafed23`, and `tests/test_windows_webview_keyboard_guidance_truth.py@69d96a063cdd88b6e6db43ca7aa04087b80bcf11`. These contain 2, 3 and 2 top-level test functions respectively.
4. Emergency STOP is *independent of an operating WebView*: `windows_entry.main(["--native-emergency-stop"])` dispatches to the real native fallback; WebView startup/storage errors offer the same keyboard-operable native Yes/No STOP choice, defaulting to no. Test module `tests/test_windows_native_emergency_stop_fallback.py@190872187d4882fa8f2b810a6f862f98f2bb8fbf` contains 12 top-level tests covering no/cancel/unknown, explicit yes durable STOP, failure/no false success, broken WebView, Unicode workspace and independent command path.
5. No bookmaker account, provider write capability, external credentials, external settlement or real-money permission was needed to inspect or implement these existing, product-integrated source semantics. PAPER/SHADOW evidence is not real-money evidence.

## Qualification truth and terminal boundary

- **Performed in this closure audit:** read live main, source, related tests and actual WebView/native launcher; checked 14 unchanged source/test Git blob identities relative to the integrated parent; performed deterministic source assertions on HTML labels/ID uniqueness, dispatch/focus/STOP bindings; inspected focused test declarations.
- **Not executed in this audit:** full `pytest`, executable Windows build/UIA run, physical keyboard/NVDA test. PR #1598 latest exact-head CI/Windows were queued/skipped or not yet qualified at audit time; **NO CI PASS is claimed**. A prior head had a failing CI; no inference of PASS for merged head.
- **Not closed:** Plan 6 §7А exact-candidate machine qualification and §7В aggregate terminal bookkeeping, §3А complete UI workflows, §5Б comprehensive package SBOM/licensing, §6А full upgrade/migration acceptance, final Plan 7/8.
- `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.

**Remaining approved implementation within the narrow 7Б source requirement: 0.** Ordinary development agents must not reimplement this accepted keyboard/STOP source scope without a documented reproducible regression, changed acceptance contract or later breakage. Future Windows test outcomes belong to §7А or final Plan 8; an actual reproduced 7Б defect may explicitly REOPEN this record.

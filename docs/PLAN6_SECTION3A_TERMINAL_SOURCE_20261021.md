# План 6 / Розділ 3А — остаточне закриття source development (2026-10-21)

**РОЗДІЛ ПЛАНУ 6 №3А — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Схвалений обсяг

Cover launch/config/source status/research/PAPER portfolio/risk/agent/models/STOP/recovery/settings/export/update flows. This is the Windows **operator-UI and canonical bridge integration** Section, not Plan-6 §5Б package SBOM/signature proof, §6А offline release lifecycle, §7А final exact Windows qualification or Plan-8 physical NVDA / external bookmaker release.

## Canonical source (postimplementation readback)

`SOURCE_MAIN_SHA=77e840a5f1e43eba489788f267f4dc00c641d367`

| Integrated file | Git blob SHA |
|---|---|
| `src/autosport/windows_web/index.html` | `32c5e92ba434bce4c5e1bf94bc96f1b33464b5f2` |
| `src/autosport/windows_web/app.js` | `d8817ad09d47e3067bddf14e57fe86697a4f8e57` |
| `src/autosport/windows_webview_shell.py` | `e09ce266764bd9763a33d71487ff38a8ffb228e6` |
| `src/autosport/windows_entry.py` | `20c0b6a993c4b1170c0200e0ad388d9452b240f1` |
| `tests/test_plan6_3a_operator_journey_wiring.py` | `b858c8eccea4a090b24d04f37cb84fadeba8c438` |
| `tests/test_plan6_3a_path_draft_preservation.py` | `9050358fb0fe5b4ac85bf971520c45dc9244581c` |
| `tests/test_plan6_3a_source_selection_truth.py` | `eba2115ac84732f6dbb6c849359a29f16d30c743` |

The source files and both new regression files were independently read back **from main after PR merges**, not from a draft branch.

## One real product path, not disconnected samples

The actual packaged Windows WebView2 HTML/JS uses `AutosportWebController` in `windows_webview_shell.py`. Independent GitHub source-text checks on the exact files above found **19/19 UI-dispatched actions mapped to 19/19 real Python action handlers**; zero missing or extra bridge actions; zero missing Python method definitions; zero duplicate DOM ids; and zero JS-referenced absent DOM ids.

- **Launch, surfaces, settings:** `windows_entry.py`, canonical WebView2, `surface.select` and the shared surface contract; live Ukrainian keyboard/focus/status presentation.
- **Dataset, research, strategies and models:** `dataset.select`, `strategy.set`, `research_plan.select`, `replay_speed.set`, `replay.run`, readback of registered agents and verified learning status via `agents.status`.
- **Source status / PAPER continuous operation:** canonical source registry, `product_source.configure`, `product_runtime.start` / `.stop`, source/economic/PaperBook readbacks; no new provider authority.
- **Portfolio, risk, manual tools:** server-sourced virtual-bank/tickets/evaluation, owner `preview` / `initialize` with confirmed bounds, manual `calculate` / `clear`.
- **Diagnostics, STOP, recovery, export and update:** `live.refresh`, native emergency STOP on browser failure, `recovery.run`, `evidence.export`, `update.diagnose` in explicit diagnostic-only UI; actual package installation/updating belongs to §6А.

All 19 backend handler bodies were independently inspected for actual canonical operations or worker dispatch, typed failures and source status; no `NotImplemented`/bare `pass` handler was identified.

## Required repairs integrated before closure

- [PR #2329](https://github.com/Oleksii-debug/Autosport/pull/2329), [#2375](https://github.com/Oleksii-debug/Autosport/pull/2375), [#2376](https://github.com/Oleksii-debug/Autosport/pull/2376): canonical GUI/Windows recovery error redaction.
- [PR #2423](https://github.com/Oleksii-debug/Autosport/pull/2423), [#2425](https://github.com/Oleksii-debug/Autosport/pull/2425): rejected surface and strategy/speed/mode selection reconciliation.
- [PR #2430](https://github.com/Oleksii-debug/Autosport/pull/2430): Windows Data.exe dispatch to canonical workspace backup/restore CLI.
- **[PR #2437](https://github.com/Oleksii-debug/Autosport/pull/2437)** merge `25a6bfb48f5f93eeab7552f81a8224a93a902090`: unsubmitted dataset/research paths survive 250-ms polling, failed/UNKNOWN selection and concurrent edits. Source-exact V8 **15/15 PASS**.
- **[PR #2438](https://github.com/Oleksii-debug/Autosport/pull/2438)** merge `77e840a5f1e43eba489788f267f4dc00c641d367`: unconfigured source no longer appears as saved, rejected selection reverts to verified state, UNKNOWN and newer edits never silently commit. Source-exact V8 **18/18 PASS**.

## Precise qualification boundary

Executed in an isolated V8 runtime on verbatim current-source JavaScript: 15/15 path-draft and 18/18 source-selection scenarios. Source inventory/mapping and Git blob readback completed. Existing regression tests for navigation rejection, focus, emergency stop, owner confirmation, Windows paths, worker rejection/recovery, source selection and the product bridge remain part of the repository.

**Not claimed:** full local Python checkout pytest, queued GitHub CI/Windows Candidate PASS, physical Windows installer acceptance, manual NVDA, real bookmaker evidence, real-money execution, completed Plan 6 or completed whole Autosport. GitHub PR checks #2437/#2438 were QUEUED or SKIPPED at last inspection, not SUCCESS. No mandatory owner physical-NVDA gate applies to this intermediate source-development Section; Plan 8 retains final acceptance.

## Terminal disposition

`remaining_approved_section_3A_source_implementation=0`
`section_status=DONE`
`closure=CLOSED_FOR_DEVELOPMENT / TERMINAL`
`REAL_MONEY_EXECUTION=false`
`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`

Normal agents must skip §3А. Reopen only for a reproduced regression, invalid source-evidence claim or materially changed scope, recorded as `REOPENED` before mutation. Other Plan-6 open sections (§5Б, §6А, §7А and §7В), Plans 7–8, full release and physical QA remain separate; they are **not** silently marked done here.

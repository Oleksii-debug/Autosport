# Autosport — Plan 6 §3В: terminal Windows focus/recovery/status closure (2026-10-21)

**РОЗДІЛ №3В (План 6) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Approved atomic scope

"Focus restoration, error recovery and actionable status are deterministic" is the entire independent §3В acceptance in `ATOMIC_OPEN_SECTIONS_2026-10-13.md` / canonical [Plan 6](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit). This is the current **integrated Windows/Tk** product workflow. The replacement WebView2 shell is separately assigned to §2А–2В / #1598, and must retain the same invariants when actually merged. No false claim that #1598 is in main.

## Existing actual implementation: REUSE, not a second shell

- `src/autosport/windows_layout.py`: the real shell chooses a declared target via `_surface_target_widget`, focuses its actual widget or the accessible details fallback in `_focus_surface_target`; `_cycle_shell_surface` restores focus to the navigation control; selection state and availability are updated together. Unsupported presentation-only/disabled surfaces cannot advertise a reachable Open action.
- `src/autosport/windows_gui.py`: `repair_workspace` stops incompatible concurrent replay/live/dataset activity with localized actionable status, quarantines the precise workspace before recovery, detaches uncertain sessions, handles teardown/configuration/start errors fail-closed and only publishes verified recovered state. `_poll_recovery_worker` handles error/no-result/unresolved/complete outcomes, without falsely reopening economic state.
- `src/autosport/gui.py`, `src/autosport/recovery_worker.py`: established product GUI state, worker lifecycle and bounded polling are reused, not forked.

## Git source and qualification receipts

The following **11/11** current-main production/test Git blobs were independently read and are byte-for-byte identical to frozen qualified source head `d93d203e867c71aad880f1b2e3d144549e166030` (PR #2261), which passed actual [CI #38148320018](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018) and [Windows Candidate #38148320047](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047), both terminal SUCCESS on that exact head:

| File | Matching blob SHA |
| --- | --- |
| `src/autosport/windows_gui.py` | `865d0dd55ea477ea42873b27ede8235508201ec6` |
| `src/autosport/windows_layout.py` | `71fb3e2f0edb05c99182fb1ec19d9455d421fd1a` |
| `src/autosport/gui.py` | `78e180e4756602c5f640c5eb8fd588b878addb34` |
| `src/autosport/recovery_worker.py` | `ff007f3a64ea03c620180c8088dc1f796dc5a381` |
| `tests/test_gui_workspace_recovery.py` | `80c2f130183259b7e3f22c1085be8aefdf6e1da6` |
| `tests/test_gui_replay_terminal_reopen.py` | `3ac6897498bb31f80e43819778a98d95f66a9d54` |
| `tests/test_windows_recovery_workspace_isolation.py` | `02c8db6cbdd067d1ed79012e8b44613ee025b085` |
| `tests/test_windows_recovery_unresolved_visibility.py` | `94ae187803cc0860083d49a1a7e00a1f708fa740` |
| `tests/test_windows_recovery_configuration_error_safety.py` | `deec504d2a6f990b68b638263bcc5b1b47078afe` |
| `tests/test_windows_recovery_teardown_failure.py` | `33405fd67d0834e59621012e7588c361bc31d40b` |
| `tests/test_windows_shell_localization.py` | `b5c313a445ad481df2dfa6793318947ec187aa2e` |

Pre-closure source was integrated on `main@92940ba0486f2d5faf8e147bcee74f8abdbcc4a6`; this receipt is documentation-only. Exact postmerge SHA should be read from GitHub; a docs-only commit does not introduce new runtime source. The current-main hosted CI of later unrelated commits is **not** called a fresh PASS. Complete local pytest was not executed: GitHub DNS resolution was unavailable in the execution container. The stated successful CI is for the exact byte-identical qualified production/test files listed above.

§3В does not claim §3А's all-workflow completeness, §2А–2В WebView2 completion, §7 plan-wide qualification, real bookmaker execution, owner testing or final physical NVDA. No new mandatory implementation remains *within this atomic current Windows/Tk focus/recovery/status scope*.

`remaining_approved_implementation_for_Plan6_3В=0`.
`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

Reopen only on a concretely demonstrated focus/recovery/status regression or changed owner-approved scope.

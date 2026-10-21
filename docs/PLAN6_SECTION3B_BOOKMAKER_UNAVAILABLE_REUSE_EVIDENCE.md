# Autosport — План 6, розділ 3Б: остаточне source-closure (2026-10-21)

**РОЗДІЛ №3Б (План 6) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL.**

## Ухвалений вузький обсяг

“Unsupported external bookmaker actions visibly unavailable, not blockers for PAPER/SHADOW product.” Canonical source contract: `ATOMIC_OPEN_SECTIONS_2026-10-13.md`, matching [Google Drive Plan 6](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit).

## Integrated functioning behavior, not a stub

The canonical existing **Windows Tk shell** reports unsupported `bookmakers_accounts` and `opportunities` as `visible-disabled`; both have `target_widget=None`, a localized Ukrainian `blocked_reason`, and remain visible in the real shell inventory. `windows_layout._render_shell_surface` exposes the disabled state and specific reason in real on-screen status and selectable details; `refresh_windows_shell_open_availability` disables the actual Open button whenever there is no mounted target. It does not fabricate an execution button. The bookmaker explanation explicitly distinguishes existing account-data read capability from the **unwired Windows account UI** and states “реальне виконання ставок вимкнене.” At the same time, active source surfaces for `market_mirror`, `research_agents`, `paper_bank`, `portfolio`, `tickets_positions`, `evaluation_learning`, `diagnostics_recovery` remain navigable and are not blocked by a missing bookmaker connector. `windows_surface_contract.load_surface_selection/save_surface_selection` preserve navigation state atomically and reject invalid/corrupt persisted identities.

No separate bookmaker engine, provider credential, irreversible stake execution, parallel GUI or standalone demo is created by this closure. This is **reuse of the currently mounted Tk Windows baseline**, not a claim the replacement WebView2 shell in PR #1598 is merged. The eventual #1598 integration must preserve the existing source truth; its own §§2А–2В and 3А/3В are separate open work.

## Exact source/test provenance

Qualified frozen head `d93d203e867c71aad880f1b2e3d144549e166030` was merged previously via PR #2261. [CI run #38148320018](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018) and [Windows Candidate #38148320047](https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047) report completed SUCCESS on that exact head, verified again on 2026-10-21. **11/11** source and test Git blobs identical on current integrated main (before this receipt `ae8376d1f35da65313fac45fe7efbca214575c35`) and that frozen qualified head:

| Actual main source/test path | Identical Git blob SHA |
|---|---|
| `src/autosport/windows_surface_contract.py` | `daebb0afaee3f89750e5692200123aeb68bada5d` |
| `src/autosport/windows_layout.py` | `71fb3e2f0edb05c99182fb1ec19d9455d421fd1a` |
| `src/autosport/windows_gui.py` | `865d0dd55ea477ea42873b27ede8235508201ec6` |
| `src/autosport/gui.py` | `78e180e4756602c5f640c5eb8fd588b878addb34` |
| `src/autosport/localization.py` | `4efd7b8ae097cece6760c3e2c0b237795953bbf1` |
| `src/autosport/windows_entry.py` | `f6510b66c17c27358f0fbaf0c81c7832bcd9f3e9` |
| `tests/test_windows_surface_contract.py` | `a396d34e7cfa82147fcca09392969aea4850887c` |
| `tests/test_windows_shell_localization.py` | `b5c313a445ad481df2dfa6793318947ec187aa2e` |
| `tests/test_windows_shell_accessibility_runtime.py` | `1490416721c0d38a76f43cdb42dea00400e143fc` |
| `tests/test_windows_layout.py` | `daf04aa91f7693bd9603bd4672c5c1b299642ecb` |
| `tests/test_gui_localization_contract.py` | `a4f58dbc0c29809df55b79575dd8583bdd932cfd` |

Qualified tests include explicit assertions on both disabled surfaces, absent action target, genuine localized blocked reason, shell truth labels, and active navigation. This no-code receipt is **not** fresh current-main CI, complete local Windows acceptance, WebView2/NVDA acceptance or real-money testing. Local complete checkout execution was unavailable (GitHub DNS); do not invent test counts or local results.

`remaining_approved_implementation_for_Plan6_3Б=0`.
`REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.

**Plan 6 3А / 3В, parent Section 3, Plan 7/8 and #1598 are explicitly NOT closed here.** Reopen 3Б only on a new requirement or a concrete demonstrated regression, e.g. a future WebView2 release incorrectly enabling an unconfigured bookmaker action.

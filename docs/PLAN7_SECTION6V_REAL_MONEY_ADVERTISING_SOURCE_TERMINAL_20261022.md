# План 7 — розділ №6В: не рекламувати реальні ставки у PAPER/SHADOW

**РОЗДІЛ №6В — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL** тільки для самостійного source-обсягу Плану 7 §6В: `No external real-money capability is advertised.`

## Acceptance / реальний користувацький шлях

Це вже реалізована в канонічному Windows WebView2/контролері межа продукту; повторної реалізації не потребує:

- `src/autosport/windows_web/index.html`: видиме українське попередження «Реальні грошові ставки вимкнено», окреме пояснення що симуляційна робота не виконує реальних ставок і кнопка «Запустити симуляційну роботу». Немає кнопки для розміщення реального ордера, внесення грошей або cashout.
- `src/autosport/windows_surface_contract.py`: `bookmakers_accounts` та `opportunities` навмисно `visible-disabled`, `target_widget=None`, із локалізованою причиною; `paper_bank` та `portfolio` залишаються активними.
- `src/autosport/windows_webview_shell.py`: канонічний WebView2 bridge явно віддає `real_money_execution=False`, `real_money_authorized=False`, `includes_live_execution_exposure=False`, `live_execution_headroom_authoritative=False`; інтерфейс не перетворює PAPER-прогноз на зовнішнє розміщення ставки.
- `src/autosport/windows_web/app.js`: не містить дієвого place-bet/place-order/cashout шляху; вибір неактивної поверхні не є торговою дією.
- `tests/test_windows_surface_contract.py` уже містить перевірку справжньої реєстрації вимкнених поверхонь, їхніх відсутніх action-target та видимих причин.

## Схема доказів

Source-baseline: `main@4e35010937a21150359d6a5cd229f7958b2dac3b`. Незмінні GitHub source/test blob SHA на момент перевірки:

| Канонічний шлях | Git blob SHA |
|---|---|
| `src/autosport/windows_surface_contract.py` | `19d80b45f4ad639682df9de01364e311d5ba2551` |
| `src/autosport/windows_web/index.html` | `32c5e92ba434bce4c5e1bf94bc96f1b33464b5f2` |
| `src/autosport/windows_web/app.js` | `44ff2bf39d76972e73038d50a7732a54004bf4ff` |
| `src/autosport/windows_webview_shell.py` | `afcd81e4477ceaf17cba536bdc239287bc36a856` |
| `tests/test_windows_surface_contract.py` | `c958f17f14703931e56bb025fde8b0a2204312af` |
| `tests/test_windows_webview_runtime_ukrainian_status.py` | `096b6c38ed101cb509bc187bf28abe7beef5e58d` |

**10/10 scoped source assertions PASS**, executed on exact authenticated GitHub source bytes: visible truth notice; simulation-only controls; absence of real-money HTML action; disabled bookmaker & opportunity targets; active PAPER & portfolio; backend false authorization/execution; no live risk exposure; checked-in negative source tests; no direct bet dispatch in app.js. These were **static source assertions**, not a complete Python, JS/browser, Windows, licensed bookmaker or physical NVDA test. Host CI may be queued; no CI/Windows PASS is inferred.

## Закриття і невідкриті залежності

`remaining_approved_implementation_plan7_6V=0` for this precise source rule. No additional implementation PR is required exclusively for §6В. Continue Plan 7 §6А/§6Б and other WAITING_UPSTREAM tasks when their explicit dependencies are met; Plan 7 as a whole is **NOT DONE**. Plan 6 §5Б/§6А/§7А/§7В and Plan 8 remain open. New bookmaker/provider, live execution and final signature/Windows/NVDA capabilities **are not unlocked** by this section close. Reopen only on a concrete reproduced regression or a new approved scope.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

# План 7 §6В — зовнішнє виконання реальних ставок не рекламується

**РОЗДІЛ №6В (атомарний План 7) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL (2026-10-22)** для раніше реалізованої **source/release truth boundary**, не для всього пакета M1 чи фінального Windows/NVDA acceptance.

Затверджений атомарний обсяг: **«No external real-money capability is advertised.»** У коді немає незавершеної функціональності, необхідної саме для цього source-контракту. Кандидат фінальної кампанії M1 і відповідні розділи 6А/6Б/усі решта Plan 7 залишаються WAITING_UPSTREAM; до них не переноситься статус DONE.

## Реально інтегрований шлях, не тимчасова демонстрація

1. Дійсний Windows WebView2 HTML показує постійний доступний текст `<p id="truth-boundary">Реальні грошові ставки вимкнено. Людську перевірку та перевірку NVDA ще не підтверджено.</p>`. Канонічний Python controller віддає operator-state `truth.real_money_execution=false`, `human_tested=false`, `nvda_verified=false`; native environment report також явно фіксує `whole_product_complete=false`.
2. Канонічний `release_package.build_windows_package` записує `BUILD_INFO.json` з жорсткими `real_money_execution=false`, `human_tested=false`, `nvda_verified=false`; generated release receipt містить ті самі truthful flags.
3. Незалежна функція `release_package._require_false_truth_labels` відхиляє будь-який truth-label, який не є **буквальним** `False`, включаючи `True` або відсутній запис, замість оголошення дозволу на ставки.
4. У реальному `WINDOWS_START_HERE.txt` й release instructions зазначено, що live observation read-only, PAPЕR використовує віртуальний банк, `Autosport-Data.exe` не має механізму букмекерських ставок, funding або withdrawal. Канонічні `bookmakers_accounts` і `opportunities` залишаються `visible-disabled`, без цілі виконання; це та сама платформа і UI, не паралельний модуль.
5. Вже наявний `tests/test_release_package_manifest_schema_integrity.py` використовує верифікаційні fixtures, які вимагають явно false truth-boundary.

## Точний GitHub source і обмеження кваліфікації

Базовий `main` для цього terminal source readback: `4e35010937a21150359d6a5cd229f7958b2dac3b`.
- `src/autosport/windows_web/index.html` blob `32c5e92ba434bce4c5e1bf94bc96f1b33464b5f2`.
- `src/autosport/windows_webview_shell.py` blob `afcd81e4477ceaf17cba536bdc239287bc36a856`.
- `src/autosport/release_package.py` blob `019a5d26afa76b0ab2ea83d052391741b6364589`.
- `WINDOWS_START_HERE.txt` blob `a2999091082332f16274e4ae04d6197a55c9f09f`.
- `tests/test_release_package_manifest_schema_integrity.py` blob `c4b228bbaece48d425e7db43ae80c6bf3babd53a`.
- `src/autosport/windows_surface_contract.py` blob `19d80b45f4ad639682df9de01364e311d5ba2551`.

Статична перевірка **9/9 source assertions PASS** на цих GitHub-байтах; відсутні зміни до production/test source. **Немає нового executable Windows-test або повного pytest PASS**, signed final artifact, physical NVDA evidence чи реального розрахунку прибутковості; ці окремі acceptance не заявлені.

`remaining_approved_implementation_plan7_6V=0` лише для явно truthful no-real-money product messaging та release metadata. Стан може бути переглянуто лише за новою вимогою або відтвореним дефектом, наприклад неправдиве повідомлення фінального пакета.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

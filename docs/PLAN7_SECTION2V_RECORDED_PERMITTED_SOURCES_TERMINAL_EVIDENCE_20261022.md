# План 7 §2В — recorded / permitted source fallback (source-scope terminal)

**РОЗДІЛ №2В (самостійний атомарний План 7) — ЗАКРИТО / DONE / CLOSED_FOR_DEVELOPMENT / TERMINAL (2026-10-22).**

Затверджений самостійний контракт: **«Use public permitted sources/recorded fixtures where live site unavailable.»** Розділ фіксує вже прийнятий режим законних/дозволених джерел і offline recorded fixtures; він **не** означає, що існує жива букмекерська інтеграція, що зовнішній сайт дозволив автоматичний збір, що кампанію M1 уже виконано або що цей модуль розміщений окремим дублем платформи.

## Уже інтегрований шлях без нової розробки

1. Канонічний `bookmaker_public_web_lab.evaluate_public_web_lab` кваліфікує лише **записані** семантичні сторінки `PublicPageFixture` з SHA-256, timestamp, exact HTTPS-origin, визначеними market/role/name/odds і детермінованою квитанцією `RECORDED_FIXTURE_ONLY`. `OFFLINE_SUPPORTED` не означає підтримку реального сайта або право відправлення ставок. Challenge, authentication, georestriction, suspend/unknown market, stale/semantic/odds drift -> недоступність чи відмова; жодного обходу сайту або мережевого виклику з fixture немає.
2. Канонічний продукт `autosport.cli` уже надає `replay PATH` з `ReplayEngine.from_jsonl`, спільними `AgentOrchestrator`, `MarketMirrorAgent`, `PaperBaselineAgent` та `PaperBook` **віртуального** банку. Це чинний локальний маршрут від recorded event до PAPER; не замінник live-bookmaker або інсталяторної Windows-перевірки.
3. Для формування правомірного історичного корпусу існує `historical_corpus.assemble_historical_corpus`, яка вимагає окремий `governance_proof_path` з explicit `licensing_or_retention_verified=true`, provenance та оприлюдненням контрольних даних. `source_rights_manifest` забезпечує explicit scope / human-approved source authorization. Ні власноруч вигадані права, ні synthetic fixtures не підвищуються до підтверджених справжніх provider feeds.
4. `windows_surface_contract` залишає `market_mirror` і `paper_bank` активними, а `bookmakers_accounts` — `visible-disabled`; основний оператор не обіцяє неіснуючого виконання.

## Readback з main до receipt

Вихідний main SHA: `d80bd7570cd4a36d5a4a9a0d476dde329a4a43f8`.

| Canonical path | Git blob SHA |
|---|---|
| `src/autosport/bookmaker_public_web_lab.py` | `292b28cff546f6c4c15dd24ff1f3e2d189b00224` |
| `tests/test_plan4_section7_public_web_lab.py` | `e0378ff6773268492d3f6e8e86b7ae50586d98f2` |
| `src/autosport/cli.py` | `5110f75aba618e6d9e32122ea1a06166b50c1d4c` |
| `src/autosport/historical_corpus.py` | `73570b1676f7c1d3729a324f79899c1143d310ee` |
| `tests/test_historical_corpus.py` | `73ff7c0f9832e3901a4705bcd8ed06b896793f11` |
| `src/autosport/source_rights_manifest.py` | `e1dfae900c1742c40f05ab99573706559822229a` |
| `src/autosport/windows_surface_contract.py` | `19d80b45f4ad639682df9de01364e311d5ba2551` |

**Фактично виконане в цьому прийманні:** **15/15 static exact-GitHub-source assertions PASS** (read-only, source checks): recorded-only boundary; fixture fields/source digest; HTTPS; challenge/auth/geo/market/semantic/odds negative gates; no bet method; deterministic fixture tests present; existing replay CLI → common PaperBook; governance and rights gates; active PAPER with disabled bookmaker surface. Це **не** запущені pytest/CI чи живий Web/Windows/E2E-тест; на цьому сеансі native runtime, Windows package, NVDA та реальні ставки **NOT_EXECUTED / NOT_VERIFIED**.

**Підсумок тільки атомарного source-контракту:** `remaining_approved_implementation_plan7_2V=0`, existing production/test source збережено без дублювання. Plan 7 §2А/2Б та інші кампанійні етапи залишаються WAITING_UPSTREAM, План 6 §5Б/6А/7А/7В незакриті, Plan8 і Windows final release окремі. Future real collection requires independent source permission and provider capability proof. Відкривати цей §2В повторно лише після відтвореного дефекту або нової вимоги.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.

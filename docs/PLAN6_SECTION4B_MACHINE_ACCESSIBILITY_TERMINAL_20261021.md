# План 6 / атомарний розділ 4Б — ЗАКРИТО ДЛЯ SOURCE-РОЗРОБКИ

CLOSED_FOR_DEVELOPMENT / TERMINAL, 2026-10-21. Точний baseline: `main@2b3aa12d9c5e26e227e510d04f76bb6bbbd32324`.

## Затверджений обсяг 4Б

Автоматизована перевірка доступних назв, семантичних ролей, клавіатурного фокуса, live-region/Windows UIA notifications, повторних AutomationId, пошкоджених/неповних provider-state і збереження текстових, вибіркових машинних звітів. Фізичний запуск Windows/NVDA, конкретний інсталятор, майбутній WebView2 з Plan6 §2 та реальні ставки не є предметом цього атомарного source-розділу.

## Інтегрована в main реалізація — точні перевірені Git blobs

- `src/autosport/accessibility_audit.py@298fc0786d59eba0448997f8e473f33d8b0f90fb`: назви, ролі, шаблони, readonly, обмеження й provider gap/stand-down, duplicate IDs, redacted UTF JSON.
- `src/autosport/keyboard_audit.py@6970481e7ff924f63942c18616bb04e0694e5859`: перевірка початкового й досяжного фокуса, клавіатурних прив'язок, звіт.
- `scripts/external_uia_audit.ps1@6c7101c509d7ce372f4550c7559924be866062da`: зовнішня AutomationElement-перевірка саме запущеного Windows продукту, duplicate/Name/ControlType/Pattern/read-only/rows, придатний для читання JSON.
- `scripts/external_uia_notification_listener.ps1@21b3f94620a643b594257f4b0e55100fa2f6a2d7`: фактична підписка на зовнішні Windows UIA NotificationEvent, process-family identity, focus before/after, позитивний event-count, обмежений буфер, редагування приватного тексту та fail-closed sentinel.
- Тести: `tests/test_accessibility_audit.py@85bd097e3f8b281baddf456e536387e6c90e2986`, `tests/test_keyboard_audit.py@567445ce9863579154b1e5413f0c5cb44282c0e9`, `tests/test_external_uia_audit_contract.py@b6f61b451c9d477d594f0da0cc92070af831cb58`, `tests/test_external_uia_notification_listener_contract.py@ab30a47de4fc35f228ae60b2725d4e7bf053c12f`.
- Пов'язані accepted merge receipts: #2289, #2299, #2316, #2317, #2328, #2335, #2342, #2346; усі вже злиті в product main; окремого активного 4Б-source PR не виявлено.

## Перевірка та точна межа доказу

20/20 незалежних **статичних read-only перевірок поточного GitHub main** пройшли: критичні назви/ролі, duplicate-ID, provider-standdown, bounded evidence, keyboard focus/report, external semantics/report, real managed UIA event subscription, event threshold, overflow, process binding, redaction, false-human-claim guards та існування негативних тестів. Це не запуск `pytest`, не робота COM на Windows і не NVDA PASS.

У recent exact-head PR #2346, #2342, #2335 GitHub Actions показує загальний workflow `completed/success`, але реально виконано тільки `superseded_run_admission`; `test` і `build` **SKIPPED**. Не називати ці CI записи успішним виконанням тестів. Верифікація exact Windows binaries/COM/NVDA належить фінальній фізичній кваліфікації Plan8, не створює нового source-scope 4Б.

Це закриття не стверджує, що відсутній WebView2 #1598 уже злитий в main: це незалежний відкритий Plan6 §2А–2В. Не переписувати 4Б при майбутній WebView2 інтеграції; новий конкретний дефект оформлювати окремо й reopening лише за протоколом.

**Рішення власника 2026-10-21:** після інтегрованого source-readback, `remaining_approved_source_implementation_for_plan6_4Б=0`. РОЗДІЛ ПЛАНУ 6 №4Б — ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL для подальшого призначення feature-роботи. Статуси Plan6 §2А–2В, §3А, §5Б, §6А–6Б та §7 не змінюються.

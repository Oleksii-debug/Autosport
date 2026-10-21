# План 6, розділ 5В — остаточне закриття розробки (fixture-signing)

**РОЗДІЛ ПЛАНУ 6 №5В — ЗАКРИТО / CLOSED_FOR_DEVELOPMENT / TERMINAL** для затвердженого обсягу **source engineering** станом на 2026-10-21. `remaining_approved_implementation=0`. Не відкривати на повторну розробку без нової вимоги або конкретного відтвореного дефекту.

## Затверджений обсяг

Інженерні примітиви підпису/перевірки релізу та оновлення з **тестовими** сертифікатами й ключами; реальний випуск продуктового сертифіката і доставка кінцевого підписаного інсталятора залишаються окремим **Планом 8**. Жодного букмекерського API, реальних ставок, торгової операції або фізичної NVDA-атестації цей розділ не заявляє.

## Інтегрована реалізація

Existing canonical [PR #2318](https://github.com/Oleksii-debug/Autosport/pull/2318) **MERGED** into `main` exactly as `f79310a9702133ff78d3f47e9a661f4428737158`; GitHub readback підтвердив `merged=true`, обидва батьківські commits та всі файли в `main`:

- `scripts/qualify_fixture_release_signature.ps1` Git blob `cecd9acfe2db35b2e2e76f5f8cb3e77495def856`: in-memory RSA-3072 fixture key, bounded exact SHA of source/archive, public-only X.509 verifier, tamper-negative source/digest/signature checks, no production-signed claim and redacted machine receipt.
- `scripts/verify_release_update_signature.ps1` Git blob `c5b3fc7ce9e4b3f453ceb854034147e9a0940b65`: independent public-certificate pin verification, signature verification, certificate validity and ZIP digest recheck, fail-closed drift.
- `.github/workflows/windows-build.yml` Git blob `32049b5229bedd883cce4bfa4e85ba0d3428cf03`: checks the actual independently verified Windows release ZIP and uploads the fixture-only evidence before subsequent Windows acceptance.

Незлитих обов'язкових source змін для цього затвердженого вузького обсягу після PR #2318 немає.

## Виконані та недоступні перевірки

- **PASS: 12/12 статичних інваріантів** на exact-main file readback: source/ZIP SHA coupling, fixture key in memory, public-only certificate, three tamper probes, independent verifier, certificate pin, digest rechecks, no production signing, no real-money/NVDA claims, workflow binding/upload, two-parent Git merge.
- **NOT_EXECUTED:** реальний Windows PowerShell/.NET запуск, ZIP-built fixture signature acceptance і зовнішні повтори/збої; середовище цього підключеного GitHub-сеансу не має PowerShell і Windows.
- **CI_NOT_EXECUTED:** exact old PR head `69955ac5ba5afb577734dd508ac060981dad33b0` jobs `39606329981` (CI) та `39606329804` (Windows Candidate) були `queued`; НЕ вважати їх PASS. Після merge інсталятор не побудовано і реальні криптографічні операції не виконані цим сеансом.
- `production_signed=false`, `real_money_execution=false`, `human_tested=false`, `nvda_verified=false`.

**Висновок:** затверджений **внутрішній код та його інтеграція** завершені; source-розробку №5В закрито за правилом owner Simplified Section Closure v3 та окремою вказівкою не блокувати закриття source відсутньою фінальною фізичною перевіркою. Реальні виконувані Windows/installer/production-signing випробування — окремі релізні приймальні докази, не нове завдання на переписування №5В.

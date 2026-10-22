# План 6 §6А — два інтегровані виправлення crash-gap recovery (2026-10-22)

**Статус розділу: IN_PROGRESS_NOT_DONE.** Це не terminal §6А і не загальний готовий Windows-реліз.

1. [PR #2527](https://github.com/Oleksii-debug/Autosport/pull/2527): після перевірки встановленого ZIP зберігається filesystem `(st_dev, st_ino)` каталогу `previous`; до й після no-replace повернення він повинен бути тим самим об'єктом. Нова перевірка відхиляє byte-identical ABA directory swap та symlink після `inspect_installed_release`. Merge `380c81003fc20ccebc717664e173431d87d72541`; на цьому main source/test blob `079d26badf1920de01e809042c8b99db253fd477` / `5c372f47f8d92a6f999ce24a31a530fb8b0579da`.
2. [PR #2528](https://github.com/Oleksii-debug/Autosport/pull/2528): після справжнього відновлення старого ZIP порожній sibling `.autosport-update-*` прибирається, щоб не блокувати наступне verified `upgrade-release`. Невідомі/неперевірені `candidate` та інші записи зберігаються для явного розгляду, не видаляються. Merge `0d1e4537a1ac1d8303860a0e12589d3e888eeb98`; точний source/test readback `c1baeb452e638740bd89fb25670013b0b1f2c9be` / `63ad7eb2e52445691253ed14f5bcf82634b8ecf1`.

**Чотири regression methods додано** у штатний `tests/test_offline_release_upgrade.py`: same-byte replacement, post-verification symlink swap, subsequent update after crash-gap recovery, preservation of unverified staged candidate. Вхідні source assertions 7/7 і 5/5 виконані на GitHub bytes, відповідно. Це **не** повний Python/Windows результат; точний командний тест: `python -m unittest -v tests.test_offline_release_upgrade`. Також потрібна Windows candidate кваліфікація точного релізного SHA.

Канонічна реалізація використовується без альтернативного updater; `REAL_MONEY_EXECUTION=false`. Розділ §6А лишається незавершеним через cross-schema migration і повне Windows release lifecycle acceptance; §5Б, §7А, §7В також відкриті. Без відтвореного дефекту не дублювати ці два вже злиті виправлення.

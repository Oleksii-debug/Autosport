from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


PRODUCT_RUNTIME_UK_UA: Mapping[str, str] = MappingProxyType(
    {
        "ui.product_runtime.button.start": "PAPER: старт",
        "ui.product_runtime.button.stop": "PAPER: стоп",
        "ui.product_runtime.status.idle": "Тривала PAPER-робота не запущена.",
        "ui.product_runtime.status.starting": "Запускається канонічна тривала PAPER-робота…",
        "ui.product_runtime.status.running": (
            "Тривала PAPER-робота активна: джерело {source_id}; циклів {cycles}; "
            "останнє успішне оновлення {last_success_at}."
        ),
        "ui.product_runtime.status.tick": (
            "Цикл {cycle_index} завершено: зафіксовано змін {committed}; доставлено {delivered}; "
            "розраховано {settled}; останнє успішне оновлення {last_success_at}."
        ),
        "ui.product_runtime.status.stopping": "Надіслано команду STOP; очікується безпечне завершення канонічної роботи.",
        "ui.product_runtime.status.stopped": "Тривалу PAPER-роботу зупинено: причина {reason}; циклів {cycles}.",
        "ui.product_runtime.status.error": (
            "Тривала PAPER-робота завершилась помилкою типу {error_type}. "
            "Робочу область заблоковано для економічних дій до перевірки відновлення."
        ),
        "ui.product_runtime.status.configuration_missing": (
            "Тривала PAPER-робота не запущена: змінна AUTOSPORT_PRODUCT_SOURCE_FACTORY не задана."
        ),
        "ui.product_runtime.status.configuration_invalid": (
            "Тривала PAPER-робота не запущена: AUTOSPORT_PRODUCT_SOURCE_FACTORY містить "
            "зайві пробіли або не відповідає точному джерелу з реєстру цієї збірки."
        ),
        "ui.product_runtime.status.recovery_required": (
            "Тривала PAPER-робота заблокована: спочатку відновіть робочу область із карантину."
        ),
        "ui.product_runtime.status.operation_busy": (
            "Спочатку завершіть поточний повтор, живе спостереження, відновлення або експорт."
        ),
        "ui.product_runtime.status.session_close_failed": (
            "Тривала PAPER-робота не запущена: поточну економічну сесію не вдалося безпечно закрити."
        ),
        "ui.product_runtime.status.start_failed": "Не вдалося запустити фонову тривалу PAPER-роботу.",
        "ui.product_runtime.status.stop_not_running": "Тривала PAPER-робота зараз не виконується.",
        "ui.product_runtime.status.close_wait": (
            "Перед закриттям Автоспорт зупиняє тривалу PAPER-роботу та очікує безпечного завершення."
        ),
        "ui.product_runtime.status.base_session_reopened": (
            "Тривалу PAPER-роботу завершено; локальну PAPER-сесію знову відкрито."
        ),
        "ui.product_runtime.status.base_session_reopen_failed": (
            "Після завершення тривалої PAPER-роботи локальну сесію не вдалося відкрити; "
            "потрібне відновлення робочої області."
        ),
        "ui.product_runtime.accessibility.start.name": "Запустити канонічну тривалу PAPER-роботу",
        "ui.product_runtime.accessibility.start.description": (
            "Запускає у фоні наявний AutonomousProductRuntime. Реальні ставки не виконуються."
        ),
        "ui.product_runtime.accessibility.stop.name": "Зупинити канонічну тривалу PAPER-роботу",
        "ui.product_runtime.accessibility.stop.description": (
            "Надсилає команду STOP канонічному AutonomousProductRuntime і чекає завершення."
        ),
        "ui.product_runtime.accessibility.status.name": "Стан тривалої PAPER-роботи",
        "ui.product_runtime.accessibility.status.description": (
            "Лише для читання: запуск, цикли, STOP або помилка канонічного продуктового виконання."
        ),
    }
)

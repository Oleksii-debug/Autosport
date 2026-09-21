from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


PRODUCT_RUNTIME_UK_UA: Mapping[str, str] = MappingProxyType(
    {
        "ui.product_runtime.frame.title": "Тривала PAPER-робота",
        "ui.product_runtime.button.start": "PAPER: старт",
        "ui.product_runtime.button.stop": "PAPER: стоп",
        "ui.product_runtime.status.idle": "Тривала PAPER-робота не запущена.",
        "ui.product_runtime.status.starting": "Запускається канонічна тривала PAPER-робота…",
        "ui.product_runtime.status.running": (
            "Тривала PAPER-робота активна: джерело {source_id}; циклів {cycles}; "
            "останнє успішне оновлення {last_success_at}."
        ),
        "ui.product_runtime.status.tick": (
            "Цикл {cycle_index} завершено: отримано delta {committed}; доставлено {delivered}; "
            "settlement {settled}; останнє успішне оновлення {last_success_at}."
        ),
        "ui.product_runtime.status.stopping": "Надіслано команду STOP; очікується безпечне завершення канонічної роботи.",
        "ui.product_runtime.status.stopped": (
            "Тривалу PAPER-роботу зупинено: причина {reason}; циклів {cycles}."
        ),
        "ui.product_runtime.status.error": (
            "Тривала PAPER-робота завершилась помилкою типу {error_type}. "
            "Workspace заблоковано для економічних дій до перевірки відновлення."
        ),
        "ui.product_runtime.status.configuration_missing": (
            "Тривала PAPER-робота не запущена: змінна AUTOSPORT_PRODUCT_SOURCE_FACTORY не задана."
        ),
        "ui.product_runtime.status.configuration_invalid": (
            "Тривала PAPER-робота не запущена: AUTOSPORT_PRODUCT_SOURCE_FACTORY містить "
            "зайві пробіли або іншу неоднозначну конфігурацію."
        ),
        "ui.product_runtime.status.recovery_required": (
            "Тривала PAPER-робота заблокована: спочатку відновіть quarantined workspace."
        ),
        "ui.product_runtime.status.operation_busy": (
            "Спочатку завершіть поточну replay/live/recovery/export операцію."
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
            "Після завершення тривалої PAPER-роботи локальну сесію не вдалося відкрити; потрібне відновлення workspace."
        ),
        "ui.product_runtime.accessibility.start.name": "Запустити канонічну тривалу PAPER-роботу",
        "ui.product_runtime.accessibility.start.description": (
            "Запускає у фоні наявний AutonomousProductRuntime. Реальні ставки не виконуються."
        ),
        "ui.product_runtime.accessibility.stop.name": "Зупинити канонічну тривалу PAPER-роботу",
        "ui.product_runtime.accessibility.stop.description": (
            "Надсилає cooperative STOP канонічному AutonomousProductRuntime і чекає завершення."
        ),
        "ui.product_runtime.accessibility.status.name": "Стан тривалої PAPER-роботи",
        "ui.product_runtime.accessibility.status.description": (
            "Лише для читання: запуск, цикли, STOP або помилка канонічного продуктового runtime."
        ),
    }
)


def product_text(key: str, **values: object) -> str:
    try:
        template = PRODUCT_RUNTIME_UK_UA[key]
    except KeyError as exc:
        raise KeyError(f"missing product runtime localization key {key!r}") from exc
    try:
        return template.format_map(values)
    except KeyError as exc:
        missing = exc.args[0]
        raise KeyError(
            f"missing product runtime localization value {missing!r} for key {key!r}"
        ) from exc

from __future__ import annotations

from types import MappingProxyType


WINDOWS_REPLAY_STOP_UK_UA = MappingProxyType(
    {
        "ui.windows.replay_stop.button": "Зупинити повтор",
        "ui.windows.replay_stop.accessibility.name": "Зупинити поточний паперовий повтор",
        "ui.windows.replay_stop.accessibility.description": (
            "Безпечно зупиняє поточний PAPER replay, поки він обробляє події, "
            "до розблокування результатів. Клавіатура: Control+S. Реальні ставки не виконуються."
        ),
        "ui.windows.replay_stop.status.requested": (
            "Запит STOP прийнято. PAPER replay буде скасовано на найближчій безпечній межі події."
        ),
        "ui.windows.replay_stop.status.stopped": (
            "PAPER replay зупинено до розблокування результатів; PaperBook і Decision Ledger "
            "залишилися BASE, а незавершений run позначено aborted_uncommitted."
        ),
        "ui.windows.replay_stop.status.unavailable": (
            "STOP уже недоступний: немає активного replay, який ще може прийняти безпечну зупинку."
        ),
        "ui.windows.replay_stop.evaluation.stopped": (
            "Повтор зупинено оператором; результат не кваліфіковано і економічний commit не створено."
        ),
        "ui.windows.replay_stop.log.requested": "Оператор запросив cooperative STOP для PAPER replay.",
        "ui.windows.replay_stop.log.stopped": (
            "Cooperative STOP завершено до result unlock; workspace знову відкрито з канонічного стану."
        ),
    }
)

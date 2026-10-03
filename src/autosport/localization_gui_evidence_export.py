from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


GUI_EVIDENCE_EXPORT_UK_UA: Mapping[str, str] = MappingProxyType(
    {
        "ui.button.export_evidence": "Експортувати докази…",
        "ui.accessibility.export_evidence.name": "Експортувати канонічні докази",
        "ui.accessibility.export_evidence.description": (
            "Зберігає метадані канонічних доказів активної робочої області без секретів. "
            "Скорочення Control+E."
        ),
        "ui.dialog.evidence_export.choose_title": "Експортувати канонічний маніфест доказів",
        "ui.status.evidence_export.operation_busy": (
            "Експорт доказів доступний після завершення поточної операції робочої області."
        ),
        "ui.status.evidence_export.recovery_busy": (
            "Експорт доказів заблоковано, поки триває відновлення робочої області."
        ),
        "ui.status.evidence_export.already_busy": "Експорт доказів уже виконується.",
        "ui.status.evidence_export.in_progress": "Експорт доказів ще виконується.",
        "ui.status.evidence_export.workspace_changed": (
            "Активна робоча область змінилася під час вибору файлу; експорт не запущено. Повторіть дію."
        ),
        "ui.status.evidence_export.destination_invalid": (
            "Місце експорту відхилено перевіркою безпеки. "
            "Збережіть файл у батьківській папці активної робочої області або в одній із папок вище. "
            "Сусідні папки не підтримуються."
        ),
        "ui.status.evidence_export.start_failed": "Фоновий експорт доказів не вдалося запустити.",
        "ui.status.evidence_export.running": (
            "Експорт канонічних доказів виконується у фоновому процесі; інтерфейс залишається доступним."
        ),
        "ui.error.evidence_export.failed": "Експорт доказів завершився помилкою: {error}",
        "ui.status.evidence_export.complete": "Експорт доказів завершено.",
        "ui.status.close.evidence_export_busy": (
            "Експорт доказів ще виконується; дочекайтеся завершення перед закриттям програми."
        ),
    }
)

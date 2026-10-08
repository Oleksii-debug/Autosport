from __future__ import annotations

from autosport.localization import text


def test_manual_calculation_evidence_presentation_is_ukrainian() -> None:
    expected = {
        "ui.windows.manual_calculation.status.success": (
            "Готово: канонічний результат і докази доступні лише для читання; "
            "real_money_execution=false."
        ),
        "ui.windows.manual_calculation.result.evidence_hash": "Хеш доказів",
        "ui.windows.manual_calculation.result.canonical_json": (
            "Канонічний JSON доказів (незмінений)"
        ),
        "ui.windows.manual_calculation.uia.result.name": (
            "Результат і докази ручного розрахунку"
        ),
        "ui.windows.manual_calculation.uia.result.description": (
            "Лише для читання: канонічний результат, припущення, попередження та хеш доказів."
        ),
    }

    for key, expected_text in expected.items():
        rendered = text(key)
        assert rendered == expected_text
        assert "evidence" not in rendered.casefold()


def test_manual_calculation_unlocalized_evidence_message_preserves_raw_value() -> None:
    raw_value = "EVIDENCE_KIND:provider/raw-17"

    rendered = text(
        "ui.windows.manual_calculation.error.unlocalized_evidence",
        value=raw_value,
    )

    assert rendered == (
        "Канонічний текст доказів не має українського представлення: "
        f"{raw_value}"
    )
    assert raw_value in rendered

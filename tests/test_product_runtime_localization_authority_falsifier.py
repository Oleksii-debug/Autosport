from __future__ import annotations

import autosport.localization as canonical_localization
import autosport.localization_product_runtime as runtime_localization


def test_product_runtime_resources_use_single_canonical_localization_authority() -> None:
    runtime_resources = runtime_localization.PRODUCT_RUNTIME_UK_UA
    runtime_keys = set(runtime_resources)
    canonical_catalog = canonical_localization.catalog()

    assert runtime_keys <= set(canonical_catalog)
    for key, value in runtime_resources.items():
        assert canonical_catalog[key] == value

    assert "PRODUCT_RUNTIME_UK_UA" not in set(
        runtime_localization.product_text.__code__.co_names
    ), "product_text still renders directly from a parallel runtime-only catalog"

    values = {
        "source_id": "provider-a",
        "cycles": 7,
        "last_success_at": "2026-09-21T16:20:00+00:00",
    }
    key = "ui.product_runtime.status.running"
    assert runtime_localization.product_text(key, **values) == canonical_localization.text(
        key,
        **values,
    )

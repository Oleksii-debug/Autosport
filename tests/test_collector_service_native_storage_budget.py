from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBudgetError,
)
from autosport.collector_service import (
    CollectorRetentionRequiredError,
    CollectorServiceConfig,
    HeadlessCollectorService,
    main,
)
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


_INSTANT = "2026-09-21T00:00:00+00:00"


class _Source:
    source_id = "budget-source"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, _checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        return ()


def _delta(*, padding: int) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id="overflow-delta",
        source_id="budget-source",
        lawful_terms_ref="terms" + ("x" * padding),
        retention_ref="retention-v1",
        stream_epoch="epoch-1",
        source_cursor="cursor-1",
        cursor_position=1,
        event_dedupe_key="event-key-1",
        event_id="event-1",
        source_payload_digest="a" * 64,
        canonical_event_digest="b" * 64,
        source_observed_at=_INSTANT,
        collector_received_at=_INSTANT,
        collector_committed_at=_INSTANT,
        desktop_available_at=_INSTANT,
    )


def _page_geometry(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        return (
            int(connection.execute("PRAGMA page_size").fetchone()[0]),
            int(connection.execute("PRAGMA page_count").fetchone()[0]),
        )
    finally:
        connection.close()


def test_service_normalizes_native_sqlite_full_to_retention_required(tmp_path: Path) -> None:
    path = tmp_path / "collector.sqlite"
    store = CollectorDeltaStore(path)
    source = _Source()
    service = HeadlessCollectorService(
        delta_store=store,
        lifecycle=ContinuousEventLifecycle(tmp_path / "catalog.json"),
        source=source,
        state_path=tmp_path / "service.json",
        run_id="run-native-budget",
        clock=lambda: _INSTANT,
        sleep=lambda _seconds: None,
    )

    # Bind the durable native ceiling only after service bootstrap so this test
    # exercises intake-time exhaustion rather than configuration-time rejection.
    page_size, page_count = _page_geometry(path)
    exact_current_budget = page_size * page_count
    bounded = CollectorDeltaStore(path, max_bytes=exact_current_budget)
    assert bounded.configured_max_bytes == exact_current_budget

    with pytest.raises(CollectorRetentionRequiredError) as caught:
        service._append_admitted_delta(_delta(padding=page_size * 32))

    assert caught.value.code == "RETENTION_REQUIRED"
    assert "native SQLite allocation ceiling" in str(caught.value)
    assert store.get("overflow-delta") is None
    assert path.stat().st_size <= exact_current_budget


def test_cli_max_store_bytes_is_native_durable_authority(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _Source()
    max_bytes = 2 * 1024 * 1024

    with patch(
        "autosport.collector_service._load_source_factory",
        return_value=lambda: source,
    ):
        exit_code = main(
            [
                "--workspace",
                str(tmp_path),
                "--source-factory",
                "ignored:factory",
                "--run-id",
                "run-cli-budget",
                "--max-cycles",
                "1",
                "--poll-seconds",
                "1",
                "--max-store-bytes",
                str(max_bytes),
            ]
        )

    assert exit_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["stop_reason"] == "max_cycles_reached"

    reopened = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    assert reopened.configured_max_bytes == max_bytes
    page_size, page_count = _page_geometry(reopened.path)
    assert page_count <= max_bytes // page_size

def test_cli_omitted_budget_reuses_durable_custom_budget_on_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _Source()
    max_bytes = 2 * 1024 * 1024
    common = [
        "--workspace",
        str(tmp_path),
        "--source-factory",
        "ignored:factory",
        "--run-id",
        "run-custom-restart",
        "--max-cycles",
        "1",
        "--poll-seconds",
        "1",
    ]

    with patch(
        "autosport.collector_service._load_source_factory",
        return_value=lambda: source,
    ):
        assert main([*common, "--max-store-bytes", str(max_bytes)]) == 0
        capsys.readouterr()
        assert main([*common, "--resume-stopped-run"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["stop_reason"] == "max_cycles_reached"
    reopened = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    assert reopened.configured_max_bytes == max_bytes


def test_cli_explicit_conflicting_restart_budget_remains_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector_deltas.json"
    CollectorDeltaStore(path, max_bytes=2 * 1024 * 1024)
    source = _Source()

    with (
        patch(
            "autosport.collector_service._load_source_factory",
            return_value=lambda: source,
        ),
        pytest.raises(
            CollectorStorageBudgetError,
            match="conflicts with durable collector max_bytes",
        ),
    ):
        main(
            [
                "--workspace",
                str(tmp_path),
                "--source-factory",
                "ignored:factory",
                "--run-id",
                "run-conflicting-budget",
                "--max-cycles",
                "1",
                "--max-store-bytes",
                str(3 * 1024 * 1024),
            ]
        )


def test_cli_fresh_omitted_budget_establishes_product_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _Source()

    with patch(
        "autosport.collector_service._load_source_factory",
        return_value=lambda: source,
    ):
        assert (
            main(
                [
                    "--workspace",
                    str(tmp_path),
                    "--source-factory",
                    "ignored:factory",
                    "--run-id",
                    "run-default-budget",
                    "--max-cycles",
                    "1",
                    "--poll-seconds",
                    "1",
                ]
            )
            == 0
        )

    capsys.readouterr()
    reopened = CollectorDeltaStore(tmp_path / "collector_deltas.json")
    assert reopened.configured_max_bytes == CollectorServiceConfig().max_store_bytes


def test_cli_service_config_uses_same_adopted_durable_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    max_bytes = 2 * 1024 * 1024
    CollectorDeltaStore(
        tmp_path / "collector_deltas.json",
        max_bytes=max_bytes,
    )
    source = _Source()
    observed: dict[str, int | None] = {}

    def capture_run(
        service: HeadlessCollectorService,
        *,
        max_cycles: int | None = None,
    ) -> None:
        observed["config"] = service.config.max_store_bytes
        observed["store"] = service.delta_store.configured_max_bytes
        observed["max_cycles"] = max_cycles

    with (
        patch(
            "autosport.collector_service._load_source_factory",
            return_value=lambda: source,
        ),
        patch.object(HeadlessCollectorService, "run", capture_run),
    ):
        assert (
            main(
                [
                    "--workspace",
                    str(tmp_path),
                    "--source-factory",
                    "ignored:factory",
                    "--run-id",
                    "run-config-budget",
                    "--max-cycles",
                    "1",
                ]
            )
            == 0
        )

    capsys.readouterr()
    assert observed == {
        "config": max_bytes,
        "store": max_bytes,
        "max_cycles": 1,
    }


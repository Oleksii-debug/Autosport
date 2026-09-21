from __future__ import annotations

from pathlib import Path

from autosport.continuous_session import SessionState
from autosport.event_lifecycle import CatalogPage
from autosport.product_gui_worker import ProductGuiWorker
from autosport.product_runtime import build_autonomous_product_runtime


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved in this test")


def _drain(worker: ProductGuiWorker):
    messages = []
    while True:
        message = worker.poll()
        if message is None:
            return messages
        messages.append(message)


def test_failed_start_after_collector_resume_cannot_leave_split_durable_lifecycle(
    tmp_path: Path,
) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-21T15:56:00+00:00",
        sleep=lambda _seconds: None,
        initial_bankroll="100",
    )

    stopped = runtime.stop("test_precondition")
    assert stopped.state is SessionState.STOPPED
    assert runtime.collector.status()["stopped_at"] is not None

    def fail_after_collector_resume() -> None:
        raise RuntimeError("injected coordinator resume failure")

    runtime.coordinator.resume = fail_after_collector_resume  # type: ignore[method-assign]

    worker = ProductGuiWorker(
        runtime_builder=lambda _workspace, _source_factory, _bankroll: runtime
    )
    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        initial_bankroll="100",
        poll_seconds=60,
    )
    assert worker.join(2)

    messages = _drain(worker)
    assert [message.kind for message in messages] == ["ERROR"]
    assert messages[0].error_type == "RuntimeError"
    assert worker.busy is False

    collector_resumed = runtime.collector.status()["stopped_at"] is None
    coordinator_stopped = runtime.coordinator.status().state is SessionState.STOPPED

    assert not (
        collector_resumed and coordinator_stopped
    ), "failed product START left collector resumed while coordinator remained durably STOPPED"

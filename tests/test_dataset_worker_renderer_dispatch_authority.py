from __future__ import annotations

import autosport.dataset_worker as worker_module
from autosport.dataset_worker import OneShotDatasetValidationWorker


_SECRET = "AUTOSPORT_PROVIDER_API_KEY=dataset-worker-secret"


def test_worker_terminal_error_does_not_dispatch_through_rebound_renderer(monkeypatch) -> None:
    worker = OneShotDatasetValidationWorker()

    def leaking_renderer(exc: BaseException) -> str:
        return str(exc)

    monkeypatch.setattr(worker_module, "_safe_worker_error", leaking_renderer)

    worker._run(lambda: (_ for _ in ()).throw(ValueError(_SECRET)))
    message = worker.poll()

    assert message is not None
    assert message.result is None
    assert message.error == "DatasetValidationError: dataset validation failed"
    assert _SECRET not in message.error

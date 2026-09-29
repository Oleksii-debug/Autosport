from __future__ import annotations

import pytest

import autosport.product_entrypoint as entrypoint


_CANARY = "S3CR3T-CLEANUP-CANARY-DO-NOT-LEAK"


class TickFailure(RuntimeError):
    pass


class SecretStopFailure(RuntimeError):
    pass


class SecretCloseFailure(RuntimeError):
    pass


class _Runtime:
    def start(self) -> object:
        return object()

    def tick(self) -> object:
        raise TickFailure("primary failure")

    def stop(self, _reason: str) -> object:
        raise SecretStopFailure(_CANARY)

    def close(self) -> None:
        raise SecretCloseFailure(_CANARY)


def test_cleanup_notes_classify_failures_without_copying_secret_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime()
    monkeypatch.setattr(
        entrypoint,
        "_validated_source",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        entrypoint,
        "build_autonomous_product_runtime",
        lambda **_kwargs: runtime,
    )
    monkeypatch.setattr(entrypoint, "_print_record", lambda *_args, **_kwargs: None)

    with pytest.raises(entrypoint.ProductRuntimeError) as raised:
        entrypoint.run_product(
            workspace="unused",
            source_factory="unused:factory",
            max_cycles=1,
            poll_seconds=0,
            install_signal_handlers=False,
        )

    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert notes.count("RuntimeError") >= 2
    assert "SecretStopFailure" not in notes
    assert "SecretCloseFailure" not in notes
    assert _CANARY not in notes

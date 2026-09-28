from __future__ import annotations

import autosport.product_entrypoint as entry


_SECRET = "AUTOSPORT_PROVIDER_API_KEY=product-entry-secret"


def test_runtime_error_type_does_not_dispatch_through_rebound_renderer(monkeypatch) -> None:
    monkeypatch.setattr(entry, "_safe_exception_type_label", lambda _exc: _SECRET)

    wrapped = entry.ProductRuntimeError(ValueError("ordinary failure"))

    assert wrapped.error_type == "ValueError"
    assert _SECRET not in wrapped.error_type


def test_start_failure_type_does_not_dispatch_through_rebound_renderer(monkeypatch, capsys) -> None:
    monkeypatch.setattr(entry, "_safe_exception_type_label", lambda _exc: _SECRET)
    monkeypatch.setattr(
        entry,
        "run_product",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("ordinary failure")),
    )

    exit_code = entry.run_product_command(
        workspace=entry.Path("workspace"),
        source_factory="provider:factory",
        initial_bankroll="10000",
        max_cycles=1,
        poll_seconds=0.0,
    )
    output = capsys.readouterr().out

    assert exit_code == 3
    assert _SECRET not in output
    assert '"error_type":"ValueError"' in output

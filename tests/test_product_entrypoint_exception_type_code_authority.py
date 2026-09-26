from __future__ import annotations

import autosport.product_entrypoint as entry


_SECRET = "AUTOSPORT_PROVIDER_API_KEY=mutated-renderer-secret"


def _hostile_renderer(_exc: BaseException) -> str:
    return _SECRET


def test_runtime_error_type_fails_closed_on_in_place_renderer_code_mutation(monkeypatch) -> None:
    renderer = entry._SAFE_EXCEPTION_TYPE_LABEL
    monkeypatch.setattr(renderer, "__code__", _hostile_renderer.__code__)

    wrapped = entry.ProductRuntimeError(ValueError("ordinary failure"))

    assert wrapped.error_type == "Exception"
    assert _SECRET not in wrapped.error_type


def test_start_failure_type_fails_closed_on_in_place_renderer_code_mutation(monkeypatch, capsys) -> None:
    renderer = entry._SAFE_EXCEPTION_TYPE_LABEL
    monkeypatch.setattr(renderer, "__code__", _hostile_renderer.__code__)
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
    assert '"error_type":"Exception"' in output

from __future__ import annotations

import runpy
import warnings

import pytest

import autosport.collector_service as collector_service


def test_python_m_entrypoint_delegates_to_composed_canonical_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, bool]] = []

    canonical_cycle = getattr(
        collector_service,
        "_collector_service_serialized_run_cycle_v2_schedule_compat",
    )
    canonical_provider_call = getattr(
        collector_service,
        "_collector_service_interruptible_bounded_provider_call_v1",
    )

    def fake_main(argv=None) -> int:
        del argv
        observed.append(
            (
                collector_service.HeadlessCollectorService.run_cycle
                is canonical_cycle,
                collector_service.HeadlessCollectorService._bounded_provider_call
                is canonical_provider_call,
            )
        )
        return 37

    monkeypatch.setattr(collector_service, "main", fake_main)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as raised:
            runpy.run_module(
                "autosport.collector_service",
                run_name="__main__",
                alter_sys=True,
            )

    assert raised.value.code == 37
    assert observed == [(True, True)]

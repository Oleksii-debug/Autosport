from __future__ import annotations

from pathlib import Path

import pytest

import autosport.continuous_observation as observation


def test_restart_status_reader_rejects_rebound_strict_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable restart truth must not late-dispatch through a rebound JSON parser."""

    status_path = tmp_path / "continuous_observation_status.json"
    status_path.write_text(
        '{"schema_version":1,"kind":"autosport_continuous_local_observation",'
        '"run_id":"prior-run","state":"running","state":"stopped"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        observation,
        "strict_json_loads",
        lambda _text: {
            "schema_version": 1,
            "kind": "autosport_continuous_local_observation",
            "run_id": "prior-run",
            "state": "stopped",
        },
    )

    with pytest.raises(ValueError, match="authority|parser|JSON|json"):
        observation._read_previous_status(status_path)

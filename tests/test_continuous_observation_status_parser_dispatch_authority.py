from __future__ import annotations

from pathlib import Path

import pytest

import autosport.continuous_observation as observation
import autosport.json_integrity as json_integrity


def _status_path(tmp_path: Path) -> Path:
    status_path = tmp_path / "continuous_observation_status.json"
    status_path.write_text(
        '{"schema_version":1,"kind":"autosport_continuous_local_observation",'
        '"run_id":"prior-run","state":"running","state":"stopped"}',
        encoding="utf-8",
    )
    return status_path


def test_restart_status_reader_rejects_rebound_strict_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable restart truth must not late-dispatch through a rebound JSON parser."""

    status_path = _status_path(tmp_path)
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


def test_restart_status_reader_rejects_in_place_json_loads_code_swap(
    tmp_path: Path,
) -> None:
    """Object identity alone must not authorize a mutated stdlib JSON executable."""

    status_path = _status_path(tmp_path)
    canonical = json_integrity.json.loads
    original_code = canonical.__code__

    def forged(_text, **_kwargs):
        return {
            "schema_version": 1,
            "kind": "autosport_continuous_local_observation",
            "run_id": "prior-run",
            "state": "stopped",
        }

    try:
        canonical.__code__ = forged.__code__
        with pytest.raises(ValueError, match="authority|parser|JSON|json"):
            observation._read_previous_status(status_path)
    finally:
        canonical.__code__ = original_code

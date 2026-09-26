from __future__ import annotations

import builtins
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


def test_restart_status_reader_rejects_rebound_stdlib_json_decoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json.loads must not late-resolve a caller-selected decoder authority."""

    status_path = _status_path(tmp_path)

    class ForgedDecoder:
        def __init__(self, **_kwargs) -> None:
            pass

        def decode(self, _text: str):
            return {
                "schema_version": 1,
                "kind": "autosport_continuous_local_observation",
                "run_id": "prior-run",
                "state": "stopped",
            }

    monkeypatch.setattr(json_integrity.json, "JSONDecoder", ForgedDecoder)

    with pytest.raises(ValueError, match="authority|parser|JSON|json"):
        observation._read_previous_status(status_path)


def test_restart_status_reader_rejects_builtin_ord_shadow_that_forges_schema_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parser helper builtins are authority: raw schema 9 must not be decoded as 1."""

    status_path = tmp_path / "continuous_observation_status.json"
    status_path.write_text(
        '{"schema_version":9,"kind":"autosport_continuous_local_observation",'
        '"run_id":"prior-run","state":"stopped"}',
        encoding="utf-8",
    )
    canonical_ord = builtins.ord
    monkeypatch.setattr(
        json_integrity,
        "ord",
        lambda _character: canonical_ord("1"),
        raising=False,
    )

    # Demonstrate the underlying strict parser would otherwise reinterpret the raw
    # unsupported schema digit while all parser helper function/code identities stay.
    assert json_integrity.strict_json_loads('{"schema_version":9}') == {
        "schema_version": 1
    }

    with pytest.raises(ValueError, match="authority|parser|JSON|json"):
        observation._read_previous_status(status_path)

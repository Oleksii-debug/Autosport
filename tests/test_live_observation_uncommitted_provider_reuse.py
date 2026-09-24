from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from autosport.live_observation import _ReplayableBatchProvider, _poll_acknowledged


@dataclass(frozen=True, slots=True)
class _Batch:
    token: str


class _AdvancingProvider:
    source_id = "reuse-falsifier"

    def __init__(self) -> None:
        self._tokens = ("never-durable-0", "later-1")
        self._offset = 0
        self.read_count = 0

    def read_batch(self, max_items: int = 1) -> _Batch:
        assert max_items == 1
        token = self._tokens[self._offset]
        self._offset += 1
        self.read_count += 1
        return _Batch(token)


class _ResettableAdvancingProvider(_AdvancingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reset_count = 0

    def reset_pending_snapshot(self) -> None:
        self._offset = 0
        self.reset_count += 1


class _AlwaysPrecommitSqliteFailure:
    def __init__(self) -> None:
        self.observed_tokens: list[str] = []

    def poll_once(self, provider: _ReplayableBatchProvider, *, max_items: int):
        self.observed_tokens.append(provider.read_batch(max_items=max_items).token)
        raise sqlite3.OperationalError("fixture pre-commit failure")


def _exhaust_precommit_retries(
    provider: _AdvancingProvider,
) -> _AlwaysPrecommitSqliteFailure:
    engine = _AlwaysPrecommitSqliteFailure()
    with pytest.raises(sqlite3.OperationalError, match="fixture pre-commit failure"):
        _poll_acknowledged(
            engine,  # type: ignore[arg-type]
            _ReplayableBatchProvider(provider),  # type: ignore[arg-type]
            max_items=1,
        )
    return engine


def test_unrewindable_provider_cannot_silently_skip_never_durable_batch_on_reuse() -> None:
    provider = _AdvancingProvider()

    engine = _exhaust_precommit_retries(provider)

    # The bounded SQLite retry correctly reuses one cached provider batch: only one
    # underlying read occurs and both persistence attempts see the same source data.
    assert engine.observed_tokens == ["never-durable-0", "never-durable-0"]
    assert provider.read_count == 1

    # After both persistence attempts fail, that first provider batch is still not
    # durable. Reusing the same provider instance must therefore either re-deliver
    # that exact batch or fail closed/quarantine the provider. Advancing directly to
    # the next batch loses never-durable source rows and creates a continuity hole.
    try:
        retry_wrapper = _ReplayableBatchProvider(provider)  # type: ignore[arg-type]
        retried = retry_wrapper.read_batch(max_items=1)
    except RuntimeError:
        return
    assert retried.token == "never-durable-0"


def test_explicit_provider_reset_hook_preserves_never_durable_batch_on_reuse() -> None:
    provider = _ResettableAdvancingProvider()

    engine = _exhaust_precommit_retries(provider)

    assert engine.observed_tokens == ["never-durable-0", "never-durable-0"]
    assert provider.read_count == 1
    assert provider.reset_count == 1

    retried = _ReplayableBatchProvider(provider).read_batch(max_items=1)  # type: ignore[arg-type]
    assert retried.token == "never-durable-0"

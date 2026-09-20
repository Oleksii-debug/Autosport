from pathlib import Path

import pytest

from autosport._provider_evaluation_semantic_gate import (
    _set_legacy_provider_semantic_bypass_for_tests,
)


# These files predate the #662 product-semantic splice and exercise provider membership,
# persistence/recovery, and PAPER transition behavior rather than semantic provenance.
# Keep their old fixture path private and narrowly scoped; all other tests see the
# production fail-closed gate.
_LEGACY_PROVIDER_SEMANTIC_FIXTURES = {
    "test_provider_evaluation_universe.py",
    "test_evaluation_universe_execution_binding.py",
    "test_evaluation_universe_crash_retry.py",
}


@pytest.fixture(autouse=True)
def _legacy_provider_semantic_fixture_bridge(request):
    enabled = Path(str(request.node.fspath)).name in _LEGACY_PROVIDER_SEMANTIC_FIXTURES
    _set_legacy_provider_semantic_bypass_for_tests(enabled)
    try:
        yield
    finally:
        _set_legacy_provider_semantic_bypass_for_tests(False)

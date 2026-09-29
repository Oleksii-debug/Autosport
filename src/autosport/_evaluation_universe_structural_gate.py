from __future__ import annotations

"""Fail closed the legacy structural-completeness denominator path.

ObservationIntakeLedger predates the production-owned complete-board capability and can
be backed by an arbitrary structural resolver.  Its cursor/range invariants remain
useful for focused legacy/intake tests, but they cannot mint production completeness.
The supported production denominator is the live CompleteGameBoardSnapshot consumer
bridge in provider_evaluation_universe.

This module deliberately keeps only a private test authorization hook.  Private module
state is application discipline, not a cryptographic sandbox; the project-wide threat
contract does not treat arbitrary mutation/import of private implementation state as a
supported consumer API.
"""

from . import evaluation_universe as universe
from .evaluation_intake import ObservationIntakeLedger


_MAX_TEST_INTAKES = 1024
_TEST_INTAKES: dict[int, ObservationIntakeLedger] = {}


def _authorize_structural_intake_for_tests(ledger: ObservationIntakeLedger) -> None:
    """Private test-only hook for legacy structural-intake regression coverage."""

    if not isinstance(ledger, ObservationIntakeLedger):
        raise TypeError("ledger must be ObservationIntakeLedger")
    if len(_TEST_INTAKES) >= _MAX_TEST_INTAKES:
        _TEST_INTAKES.pop(next(iter(_TEST_INTAKES)))
    _TEST_INTAKES[id(ledger)] = ledger


def _is_test_authorized(ledger: ObservationIntakeLedger) -> bool:
    return _TEST_INTAKES.get(id(ledger)) is ledger


def _is_provider_consumer_adapter(ledger: ObservationIntakeLedger) -> bool:
    # Import lazily to avoid an import cycle while the package guard is installed.
    try:
        from . import provider_evaluation_universe as provider_consumer
    except ImportError:
        return False
    adapter_type = getattr(provider_consumer, "_ProviderIntakeLedger", None)
    return adapter_type is not None and type(ledger) is adapter_type


def _install_guard() -> None:
    original_build = universe.build_frozen_universe
    original_store_init = universe.EvaluationUniverseStore.__init__
    if getattr(original_build, "_production_structural_intake_denied", False):
        return

    def build_frozen_universe(*, intake_ledger, **kwargs):
        if not _is_test_authorized(intake_ledger):
            raise universe.EvaluationUniverseError(
                "structural ObservationIntakeLedger cannot authorize production completeness; "
                "use build_frozen_universe_from_complete_game_board with the live canonical provider capability"
            )
        return original_build(intake_ledger=intake_ledger, **kwargs)

    def store_init(
        self,
        workspace,
        *,
        intake_ledger,
        paper_resolver=None,
        authority_root=None,
    ):
        if not (
            _is_test_authorized(intake_ledger)
            or _is_provider_consumer_adapter(intake_ledger)
        ):
            raise universe.EvaluationUniverseError(
                "structural ObservationIntakeLedger cannot authorize durable production denominator state"
            )
        return original_store_init(
            self,
            workspace,
            intake_ledger=intake_ledger,
            paper_resolver=paper_resolver,
            authority_root=authority_root,
        )

    setattr(build_frozen_universe, "_production_structural_intake_denied", True)
    setattr(store_init, "_production_structural_intake_denied", True)
    universe.build_frozen_universe = build_frozen_universe
    universe.EvaluationUniverseStore.__init__ = store_init


_install_guard()
del _install_guard

__all__: list[str] = []

from __future__ import annotations

from datetime import datetime, timezone
import subprocess
import sys
import textwrap

import pytest

from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment_id": "env-1",
        "episode_id": "episode-1",
        "action_id": "action-1",
        "outcome_id": "outcome-1",
        "reward_id": "reward-1",
        "transition_id": "transition-1",
        "policy_id": "policy-1",
        "model_id": "model-1",
        "strategy_id": "strategy-1",
        "config_sha256": SHA_A,
        "protocol_sha256": SHA_B,
        "economic_goal_fingerprint": SHA_C,
        "risk_fingerprint": SHA_D,
        "bankroll_id": "bankroll-1",
        "portfolio_identity": "portfolio-1",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": SHA_E,
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "authority_refs": (
            AuthorityRef(
                family="campaign-economics",
                evidence_id="econ-1",
                sha256=SHA_A,
            ),
        ),
    }
    values.update(overrides)
    return values


class ForgedPolicyUtilityEvidence(PolicyUtilityEvidence):
    pass


def test_store_rejects_subclass_before_any_durable_publication(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    store = PolicyUtilityStore(path)

    first = PolicyUtilityEvidence(**_values())  # type: ignore[arg-type]
    assert store.append(first) is True
    before = path.read_bytes()

    forged = ForgedPolicyUtilityEvidence(  # type: ignore[arg-type]
        **_values(episode_id="episode-2")
    )
    with pytest.raises(PolicyUtilityError, match="exact PolicyUtilityEvidence"):
        store.append(forged)

    assert path.read_bytes() == before
    assert PolicyUtilityStore(path).list() == (first,)

    canonical = PolicyUtilityEvidence(  # type: ignore[arg-type]
        **_values(episode_id="episode-2")
    )
    assert store.append(canonical) is True
    after = path.read_bytes()
    assert store.append(canonical) is False
    assert path.read_bytes() == after
    assert PolicyUtilityStore(path).get(canonical.evidence_id) == canonical


def test_public_module_reload_preserves_fail_before_publish_exact_type_fence() -> None:
    script = textwrap.dedent(
        """
        from datetime import datetime, timezone
        import importlib
        import tempfile
        from pathlib import Path

        import autosport.policy_utility_evidence as utility

        utility = importlib.reload(utility)
        sha_a = "a" * 64
        sha_b = "b" * 64
        sha_c = "c" * 64
        sha_d = "d" * 64
        sha_e = "e" * 64

        def values(**overrides):
            data = {
                "environment_id": "env-1",
                "episode_id": "episode-1",
                "action_id": "action-1",
                "outcome_id": "outcome-1",
                "reward_id": "reward-1",
                "transition_id": "transition-1",
                "policy_id": "policy-1",
                "model_id": "model-1",
                "strategy_id": "strategy-1",
                "config_sha256": sha_a,
                "protocol_sha256": sha_b,
                "economic_goal_fingerprint": sha_c,
                "risk_fingerprint": sha_d,
                "bankroll_id": "bankroll-1",
                "portfolio_identity": "portfolio-1",
                "utility_definition_family": "owner-net-utility",
                "utility_definition_version": "v1",
                "utility_definition_sha256": sha_e,
                "completeness": utility.UtilityCompleteness.INCOMPLETE,
                "truth_class": utility.UtilityTruthClass.OBSERVED,
                "decision_kind": utility.DecisionKind.POSITIONED,
                "available_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
                "authority_refs": (
                    utility.AuthorityRef(
                        family="campaign-economics",
                        evidence_id="econ-1",
                        sha256=sha_a,
                    ),
                ),
            }
            data.update(overrides)
            return data

        class Forged(utility.PolicyUtilityEvidence):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utility.jsonl"
            store = utility.PolicyUtilityStore(path)
            first = utility.PolicyUtilityEvidence(**values())
            assert store.append(first) is True
            before = path.read_bytes()

            forged = Forged(**values(episode_id="episode-2"))
            try:
                store.append(forged)
            except utility.PolicyUtilityError as exc:
                assert "exact PolicyUtilityEvidence" in str(exc)
            else:
                raise AssertionError("reloaded store accepted polymorphic evidence")

            assert path.read_bytes() == before
            exact = utility.PolicyUtilityEvidence(**values(episode_id="episode-2"))
            assert store.append(exact) is True
            published = path.read_bytes()
            assert store.append(exact) is False
            assert path.read_bytes() == published
            assert utility.PolicyUtilityStore(path).get(exact.evidence_id) == exact
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_meta_path_mutation_cannot_disable_intrinsic_exact_type_admission() -> None:
    script = textwrap.dedent(
        """
        from datetime import datetime, timezone
        import importlib
        import sys
        import tempfile
        from pathlib import Path

        import autosport.policy_utility_evidence as utility

        assert "autosport._policy_utility_store_exact_type_guard" not in sys.modules

        class FakeMarkerFinder:
            __autosport_policy_utility_reload_finder__ = True

            def find_spec(self, fullname, path, target=None):
                return None

        sys.meta_path.insert(0, FakeMarkerFinder())
        sys.meta_path[:] = [
            finder
            for finder in sys.meta_path
            if type(finder).__module__ != "autosport._policy_utility_store_exact_type_guard"
        ]
        utility = importlib.reload(utility)

        sha_a = "a" * 64
        sha_b = "b" * 64
        sha_c = "c" * 64
        sha_d = "d" * 64
        sha_e = "e" * 64

        def values(**overrides):
            data = {
                "environment_id": "env-1",
                "episode_id": "episode-1",
                "action_id": "action-1",
                "outcome_id": "outcome-1",
                "reward_id": "reward-1",
                "transition_id": "transition-1",
                "policy_id": "policy-1",
                "model_id": "model-1",
                "strategy_id": "strategy-1",
                "config_sha256": sha_a,
                "protocol_sha256": sha_b,
                "economic_goal_fingerprint": sha_c,
                "risk_fingerprint": sha_d,
                "bankroll_id": "bankroll-1",
                "portfolio_identity": "portfolio-1",
                "utility_definition_family": "owner-net-utility",
                "utility_definition_version": "v1",
                "utility_definition_sha256": sha_e,
                "completeness": utility.UtilityCompleteness.INCOMPLETE,
                "truth_class": utility.UtilityTruthClass.OBSERVED,
                "decision_kind": utility.DecisionKind.POSITIONED,
                "available_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
                "authority_refs": (
                    utility.AuthorityRef(
                        family="campaign-economics",
                        evidence_id="econ-1",
                        sha256=sha_a,
                    ),
                ),
            }
            data.update(overrides)
            return data

        class Forged(utility.PolicyUtilityEvidence):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utility.jsonl"
            store = utility.PolicyUtilityStore(path)
            exact = utility.PolicyUtilityEvidence(**values())
            assert store.append(exact) is True
            before = path.read_bytes()

            forged = Forged(**values(episode_id="episode-2"))
            try:
                store.append(forged)
            except utility.PolicyUtilityError as exc:
                assert "exact PolicyUtilityEvidence" in str(exc)
            else:
                raise AssertionError("meta-path mutation disabled intrinsic exact-type admission")

            assert path.read_bytes() == before
            canonical = utility.PolicyUtilityEvidence(**values(episode_id="episode-2"))
            assert store.append(canonical) is True
            published = path.read_bytes()
            assert store.append(canonical) is False
            assert path.read_bytes() == published
            assert utility.PolicyUtilityStore(path).get(canonical.evidence_id) == canonical
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

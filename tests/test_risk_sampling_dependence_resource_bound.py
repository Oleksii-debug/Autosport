import unittest

from autosport.risk_sampling_dependence import (
    IidSamplingOccurrence,
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_occurrences,
)


class _OverlongOccurrenceIterable:
    """Expose one overrun sentinel and fail if the verifier keeps draining input."""

    def __init__(self, values):
        self.values = values
        self.read_count = 0

    def __iter__(self):
        for value in self.values:
            self.read_count += 1
            yield value
        self.read_count += 1
        yield self.values[-1]
        raise AssertionError("verifier read beyond fixed-N overrun sentinel")


class RiskSamplingDependenceResourceBoundTests(unittest.TestCase):
    def _structure(self):
        return ResolvedFixedNIidSamplingStructure(
            experiment_id="bounded-occurrence-read",
            membership_design_sha256="1" * 64,
            membership_protocol_record_sha256="2" * 64,
            membership_dataset_record_sha256="3" * 64,
            membership_causal_cutoff="2026-09-01T00:00:00+00:00",
            membership_precommitted_at="2026-09-02T00:00:00+00:00",
            membership_outcome_reveal_after="2026-09-10T00:00:00+00:00",
            research_protocol_id="risk-protocol",
            protocol_sha256="4" * 64,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256="5" * 64,
            sampling_frame_sha256="6" * 64,
            initial_capital_state_sha256="7" * 64,
            stake_policy_sha256="8" * 64,
            horizon_sha256="9" * 64,
            rng_algorithm="PCG64",
            rng_version="v1",
            randomization_root_sha256="a" * 64,
            planned_member_ids=("run-1", "run-2", "run-3"),
            member_stream_sha256=("b" * 64, "c" * 64, "d" * 64),
            manifest_sha256="e" * 64,
        )

    def _occurrences(self, structure):
        return tuple(
            IidSamplingOccurrence(
                member_id=member_id,
                member_index=index,
                stream_sha256=structure.member_stream_sha256[index],
                draw_transcript_sha256=f"{index + 1:064x}",
                initial_capital_state_sha256=structure.initial_capital_state_sha256,
                sampling_frame_sha256=structure.sampling_frame_sha256,
                protocol_sha256=structure.protocol_sha256,
                stake_policy_sha256=structure.stake_policy_sha256,
                horizon_sha256=structure.horizon_sha256,
                complete=True,
            )
            for index, member_id in enumerate(structure.planned_member_ids)
        )

    def test_overlong_iterable_is_bounded_at_planned_n_plus_one(self):
        structure = self._structure()
        values = self._occurrences(structure)
        occurrences = _OverlongOccurrenceIterable(values)

        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "all precommitted fixed-N members must complete",
        ):
            inspect_fixed_n_iid_occurrences(structure, occurrences)

        self.assertEqual(occurrences.read_count, structure.planned_n + 1)


if __name__ == "__main__":
    unittest.main()

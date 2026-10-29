import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import ObservedPerformance, OpponentIntelligenceStore
from autosport.participant_identity import AliasRecord, EntityIdentity, EntityKind, ParticipantIdentityRegistry
from canonical_history_bridge import audit, validated_rows

T0 = '2026-01-01T00:00:00Z'
T1 = '2026-01-03T00:00:00Z'
T2 = '2026-01-04T00:00:00Z'
SHA = 'a' * 64


def fixture():
    return {'fixture_id': 'game', 'sport': 'table_tennis', 'home': 'A', 'away': 'B', 'league': 'L',
            'status': 'finished', 'periods': [{'score': '11-8'}] * 3,
            'final_score': '11-8,11-8,11-8', 'finished_at': '2026-01-02T10:00:00Z',
            'updated_at': '2026-01-02T10:01:00Z'}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = ParticipantIdentityRegistry.initialize_pristine(self.root / 'identity.json')
        self.raw = json.dumps({'sport': 'table_tennis', 'results': [fixture()]}).encode()
        self.receipt = {'sha256': hashlib.sha256(self.raw).hexdigest(), 'received_at': T1,
                        'url': 'https://moviodds.com/results?sport=table_tennis', 'rights_verified': False}

    def add_entity(self, name, kind):
        self.registry.add_entity(EntityIdentity(name, kind, 'provider:' + name, SHA, T0, T0))
        self.registry.add_alias(AliasRecord('moviodds.com', name, name, T0, None, T0, SHA, T0))

    def test_unresolved_and_tampered_data_are_blocked(self):
        report = audit(self.raw, self.receipt, self.registry, recorded_at=T2)
        self.assertEqual(report['blocked']['unresolved_canonical_identity'], 1)
        self.assertEqual(report['candidate_count'], 0)
        self.assertFalse(report['training_authorized'])
        with self.assertRaises(ValueError):
            audit(self.raw + b' ', self.receipt, self.registry, recorded_at=T2)
        with self.assertRaises(ValueError):
            audit(self.raw, self.receipt, self.registry, recorded_at=T0)

    def test_candidate_reuses_canonical_store_and_recorded_cutoff(self):
        for name, kind in [('A', EntityKind.PARTICIPANT), ('B', EntityKind.PARTICIPANT), ('L', EntityKind.LEAGUE)]:
            self.add_entity(name, kind)
        report = audit(self.raw, self.receipt, self.registry, recorded_at=T2)
        self.assertEqual(report['candidate_count'], 1)
        self.assertFalse(report['training_authorized'])
        payload = dict(report['candidates'][0]); payload['truth'] = EvidenceTruth(payload['truth'])
        store = OpponentIntelligenceStore.initialize_pristine(self.root / 'store.json', self.registry)
        store.record_performance(ObservedPerformance(**payload))
        self.assertEqual(len(store.graph_edges(as_of=T1)), 0)
        edges = store.graph_edges(as_of=T2)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].score, '1')
        self.assertEqual(edges[0].subject_entity_id, 'A')

    def test_invalid_sets_and_score_mismatch_excluded(self):
        row = fixture(); row['periods'][0]['score'] = '12-8'
        row['final_score'] = '12-8,12-8,12-8'
        valid, reasons = validated_rows({'sport': 'table_tennis', 'results': [row]})
        self.assertEqual(valid, []); self.assertEqual(reasons['invalid_result_or_time'], 1)
        row = fixture(); row['final_score'] = '0-3'
        self.assertEqual(validated_rows({'sport': 'table_tennis', 'results': [row]})[1]['conflicting_final_score'], 1)
        with self.assertRaises(ValueError):
            validated_rows({'sport': 'table_tennis', 'results': [fixture(), copy.deepcopy(fixture())]})


if __name__ == '__main__':
    unittest.main()

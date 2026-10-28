import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from additional_capture import choose
from completion_capture import make_state

class CompletionTests(unittest.TestCase):
    def test_prospective_disjoint_selection_and_state(self):
        now=datetime.now(timezone.utc)
        rows=[]
        for i in range(5):
            rows.append({'id':str(i),'home':'A'+str(i),'away':'B'+str(i),'league':'L',
                'sport':'table_tennis','in_play':False,'kickoff_utc':(now+timedelta(minutes=5+i)).isoformat(),
                'quotes':[{'group_id':'920000','line':'To Win','side':s+str(i),'odds_dec':2} for s in ['A','B']],
                'group_status':{'920000':{'polled_at':now.isoformat()}}})
        original={'matches':[rows[0]]}; frozen=deepcopy(original)
        rows[1]['in_play']=True
        selection=choose({'matches':rows},original,now,count=3)
        state=make_state(selection)
        self.assertEqual([m['id'] for m in state['matches']],['2','3','4'])
        self.assertEqual(original,frozen)
        self.assertTrue(all(not m['frames'] and m['result'] is None for m in state['matches']))
        self.assertFalse(state['recording_complete'])
        with self.assertRaises(ValueError):make_state({**selection,'matches':selection['matches'][:2]})

if __name__=='__main__':unittest.main()

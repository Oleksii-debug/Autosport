"""Capture three additional prospective fixtures; preserve the original fifty untouched."""
import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import additional_capture
import record

ROOT = Path(__file__).parent

def fetch(path, params):
    req = Request(record.BASE + path + '?' + urlencode(params), headers={
        'User-Agent': 'Autosport-completion-study/1.0', 'Accept-Encoding': 'gzip'})
    with urlopen(req, timeout=25) as r:
        raw = r.read(30 * 1024 * 1024 + 1)
        if r.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError('oversize response')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()

def make_state(selection):
    if len(selection['matches']) != 3 or len({str(m['id']) for m in selection['matches']}) != 3:
        raise ValueError('exactly three distinct supplementary matches required')
    return {'schema':'autosport.table_tennis_recording.v1', 'source':record.BASE,
        'source_documentation':record.BASE+'/doc', 'selection_captured_at':selection['captured_at'],
        'poll_seconds':20, 'frame_seconds':180, 'errors':[], 'recording_complete':False,
        'coverage_complete':False, 'purpose':'supplement original fifty, never rewrite or hide unresolved matches',
        'matches':[{'id':str(m['id']), 'home':m['home'], 'away':m['away'],
            'league':m['league'], 'scheduled_at':m['kickoff_utc'],
            'prematch':{'captured_at':selection['captured_at'], 'odds':record.market(m)},
            'first_live_at':None, 'live_id':None, 'status':'Очікується', 'frames':[], 'result':None}
            for m in selection['matches']]}

def main():
    output=ROOT/'completion-recording.json'
    selection_path=ROOT/'completion-selection.json'
    if output.exists() or selection_path.exists():
        raise ValueError('existing evidence must not be overwritten by a fresh selection')
    exclusions=json.loads((ROOT/'completion-exclusions.json').read_text())
    record.fetch=fetch
    for attempt in range(5):
        try:
            board,digest=fetch('/odds',{'sport':'table_tennis'})
            selection=additional_capture.choose(board,exclusions,datetime.now(timezone.utc),count=3)
            selection['source_snapshot_sha256']=digest
            break
        except (OSError,ValueError) as e:
            print(json.dumps({'selection_attempt':attempt+1,'error':type(e).__name__}),flush=True)
            if attempt==4: raise
            time.sleep(30)
    state=make_state(selection)
    record.save(selection_path,selection);record.save(output,state)
    print(json.dumps({'selected':3,'matches':[(m['home'],m['away'],m['scheduled_at']) for m in state['matches']]}),flush=True)
    deadline=time.monotonic()+16200
    with ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            try:
                bj=pool.submit(fetch,'/odds',{'sport':'table_tennis'})
                rj=pool.submit(fetch,'/results',{'sport':'table_tennis'})
                board,digest=bj.result();outcomes,_=rj.result()
                record.cycle(state,board,outcomes,now=datetime.now(timezone.utc),board_sha=digest)
                print(json.dumps({'checked_at':state['last_checked_at'],'started':sum(bool(m['first_live_at']) for m in state['matches']),'finished':sum(bool(m['result']) for m in state['matches'])}),flush=True)
            except (OSError,ValueError,KeyError) as e:
                state['errors'].append({'observed_at':datetime.now(timezone.utc).isoformat(),'type':type(e).__name__})
            record.save(output,state)
            if state['recording_complete'] or time.monotonic()>=deadline: break
            time.sleep(20)

if __name__=='__main__': main()

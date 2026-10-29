"""Capture historical closing responses for the pre-specified repeated pairs.

One request per second at most, no credentials, no retries. Each raw response
and capture receipt is saved separately and digest-checked when resumed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def selected_ids(evaluation):
    ids = [str(r['fixture_id']) for r in evaluation['rows'] if r['prior_meetings'] > 0]
    if len(ids) != len(set(ids)) or any(not x.isascii() or not x.isdigit() for x in ids):
        raise ValueError('fixture IDs must be unique ASCII digits')
    return sorted(ids)


def capture(evaluation, output, *, fetch=None, delay=1.0):
    if delay < 1:
        raise ValueError('minimum delay is one second')
    output.mkdir(parents=True, exist_ok=True)
    ids = selected_ids(evaluation)
    done = 0
    for ident in ids:
        raw_path, receipt_path = output / (ident + '.json'), output / (ident + '.receipt.json')
        if raw_path.exists() or receipt_path.exists():
            if not raw_path.is_file() or not receipt_path.is_file():
                raise ValueError('incomplete prior capture; preserve and inspect before resuming')
            receipt = json.loads(receipt_path.read_text(encoding='utf8'))
            if receipt.get('sha256') != hashlib.sha256(raw_path.read_bytes()).hexdigest():
                raise ValueError('prior capture digest mismatch')
            done += 1
            continue
        url = 'https://moviodds.com/closing?' + urllib.parse.urlencode({'fi': ident})
        if fetch is None:
            with urllib.request.urlopen(url, timeout=20) as response:
                raw = response.read(2_000_001)
        else:
            raw = fetch(url)
        if len(raw) > 2_000_000:
            raise ValueError('response exceeds limit')
        data = json.loads(raw)
        if data.get('fixture_id') != ident or not isinstance(data.get('selections'), list):
            raise ValueError('unexpected fixture or schema')
        captured = datetime.now(timezone.utc).isoformat()
        with raw_path.open('xb') as f:
            f.write(raw)
        receipt = {'url': url, 'received_at': captured, 'sha256': hashlib.sha256(raw).hexdigest(),
                   'bytes': len(raw), 'rights_verified': False}
        with receipt_path.open('x', encoding='utf8') as f:
            json.dump(receipt, f, indent=2)
            f.write('\n')
        done += 1
        print(f'{done}/{len(ids)}', flush=True)
        if fetch is None:
            time.sleep(delay)
    return {'selected': len(ids), 'captured': done}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('evaluation', type=Path)
    p.add_argument('output_directory', type=Path)
    args = p.parse_args()
    try:
        result = capture(json.loads(args.evaluation.read_text(encoding='utf8')), args.output_directory)
    except Exception as exc:
        # Do not print provider response bodies, credentials, or headers.
        print('capture stopped:', type(exc).__name__)
        raise SystemExit(1)
    print(json.dumps(result))


if __name__ == '__main__':
    main()

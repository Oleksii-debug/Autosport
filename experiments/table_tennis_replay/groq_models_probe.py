"""One bounded read-only Groq capability probe; never logs credentials.

No fallback account, retry, inference or trading. The secret file is supplied
explicitly and must remain outside version control.
"""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

ENDPOINT = 'https://api.groq.com/openai/v1/models'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(key, opener=None):
    if not isinstance(key, str) or not key.startswith('gsk_') or not key.isascii() or any(c.isspace() for c in key):
        return {'status': 'INVALID_CREDENTIAL_FORMAT'}
    opener = opener or build_opener(NoRedirect())
    request = Request(ENDPOINT, headers={'Authorization': 'Bearer ' + key})
    try:
        with opener.open(request, timeout=10) as response:
            data = response.read(1_000_001)
        if len(data) > 1_000_000:
            return {'status': 'OVERSIZE_RESPONSE'}
        payload = json.loads(data)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            return {'status': 'INVALID_RESPONSE'}
        models = payload['data']
        if any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in models):
            return {'status': 'INVALID_RESPONSE'}
        ids = sorted(set(m['id'] for m in models))
        if any(key in v or len(v) > 256 for v in ids):
            return {'status': 'INVALID_RESPONSE'}
        return {'status': 'MODELS_AVAILABLE', 'models': ids, 'free_tier_verified': False}
    except HTTPError as exc:
        return {'status': 'HTTP_ERROR', 'http_code': exc.code}
    except (URLError, TimeoutError, OSError):
        return {'status': 'NETWORK_UNAVAILABLE'}
    except (ValueError, UnicodeError):
        return {'status': 'INVALID_RESPONSE'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--credentials-file', type=Path, required=True)
    p.add_argument('--profile', type=int, choices=(1, 2, 3, 4), required=True)
    args = p.parse_args()
    try:
        keys = json.loads(args.credentials_file.read_text('utf-8'))['keys']
        if not isinstance(keys, list):
            raise ValueError()
        key = keys[args.profile-1]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        print(json.dumps({'status': 'CREDENTIALS_UNAVAILABLE'}))
        return 2
    report = probe(key)
    print(json.dumps({'profile': args.profile, **report}, ensure_ascii=False))
    return 0 if report['status'] == 'MODELS_AVAILABLE' else 2


if __name__ == '__main__':
    raise SystemExit(main())

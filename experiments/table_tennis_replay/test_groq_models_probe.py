import io
import unittest
from urllib.error import HTTPError, URLError
from groq_models_probe import probe, NoRedirect, ENDPOINT


class Fake:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, 0
    def open(self, request, timeout):
        self.calls += 1
        assert request.full_url == ENDPOINT and timeout == 10
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


class ProbeTests(unittest.TestCase):
    def test_models_and_no_free_tier_claim(self):
        f = Fake(b'{"data":[{"id":"model-a"}]}')
        r = probe('gsk_test_only', f)
        self.assertEqual(r['models'], ['model-a'])
        self.assertFalse(r['free_tier_verified'])
        self.assertEqual(f.calls, 1)

    def test_secret_in_errors_never_logged_and_no_retry(self):
        for error in [URLError('gsk_test_only'), HTTPError(ENDPOINT, 429, 'gsk_test_only', {}, None)]:
            f = Fake(error=error)
            r = probe('gsk_test_only', f)
            self.assertNotIn('gsk_test_only', str(r))
            self.assertEqual(f.calls, 1)

    def test_redirect_size_and_bad_data(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid'))
        for data in [b'{}', b'[]', b'{"data":[null]}', b'{"data":[{"id":"gsk_test_only"}]}']:
            self.assertEqual(probe('gsk_test_only', Fake(data))['status'], 'INVALID_RESPONSE')
        self.assertEqual(probe('gsk_test_only', Fake(b'x'*1_000_001))['status'], 'OVERSIZE_RESPONSE')


if __name__ == '__main__':
    unittest.main()

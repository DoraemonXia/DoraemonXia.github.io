import io
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

import update_papers as updater


def response(payload=b'{"data": [{"title": "Example"}]}'):
    result = MagicMock()
    result.__enter__.return_value = io.BytesIO(payload)
    return result


def error(code, headers=None):
    return HTTPError(updater.API_URL, code, 'test', headers or {}, None)


class FetchTests(unittest.TestCase):
    @patch.object(updater.time, 'sleep')
    @patch.object(updater, 'urlopen')
    def test_rate_limit_then_success(self, open_url, sleep):
        open_url.side_effect = [error(429, {'Retry-After': '120'}), response()]
        self.assertEqual(updater.fetch_papers(), [{'title': 'Example'}])
        self.assertGreaterEqual(sleep.call_args.args[0], 120)

    @patch.object(updater.time, 'sleep')
    @patch.object(updater, 'urlopen')
    def test_exhausted_retries_preserve_site_and_fail(self, open_url, sleep):
        open_url.side_effect = error(429)
        with patch.object(updater, 'update_index') as update:
            with self.assertRaises(SystemExit) as raised:
                updater.main()
            self.assertEqual(raised.exception.code, 1)
            update.assert_not_called()
        self.assertEqual(open_url.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    @patch.object(updater.time, 'sleep')
    @patch.object(updater, 'urlopen')
    def test_permanent_error_does_not_retry(self, open_url, sleep):
        open_url.side_effect = error(401)
        self.assertIsNone(updater.fetch_papers())
        sleep.assert_not_called()
        self.assertEqual(open_url.call_count, 1)

    @patch.object(updater, 'urlopen')
    def test_invalid_response_is_not_an_empty_papers_list(self, open_url):
        open_url.return_value = response(b'{"error": "unavailable"}')
        self.assertIsNone(updater.fetch_papers())

    @patch.dict(updater.os.environ, {'SEMANTIC_SCHOLAR_API_KEY': 'test-key'})
    @patch.object(updater, 'urlopen', return_value=response())
    def test_optional_api_key(self, open_url):
        updater.fetch_papers()
        self.assertEqual(open_url.call_args.args[0].get_header('X-api-key'), 'test-key')

    @patch.object(updater.random, 'uniform', return_value=0)
    def test_backoff_and_retry_after(self, jitter):
        self.assertEqual([updater.retry_delay(i) for i in range(5)], [30, 60, 120, 240, 300])
        self.assertEqual(updater.retry_delay(0, 'invalid'), 30)
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=180))
        self.assertGreater(updater.retry_delay(0, retry_at), 175)


if __name__ == '__main__':
    unittest.main()

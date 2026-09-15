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


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.known = updater.load_known_papers()
        self.published = {'title': 'Deciphering RNA–ligand binding specificity with GerNA-Bind',
                          'year': 2025, 'citationCount': 7,
                          'externalIds': {'DOI': '10.1038/s42256-025-01154-z'}}
        self.preprint = {'title': 'GerNA-Bind: Geometric-enhanced RNA-ligand Binding Specificity Prediction with Deep Learning',
                         'year': 2025, 'citationCount': 100,
                         'externalIds': {'DOI': '10.1101/2025.02.15.638393'}}

    def test_versions_merge_in_either_order_and_prefer_publication(self):
        for papers in ([self.preprint, self.published], [self.published, self.preprint]):
            result = updater.prepare_papers(papers, self.known)
            gerna = [p for p in result if p.get('_known_key') == 'doi:10.1038/s42256-025-01154-z']
            self.assertEqual(len(gerna), 1)
            self.assertEqual(gerna[0]['citationCount'], 7)

    def test_missing_odesign_is_retained_with_authors_and_year(self):
        result = updater.prepare_papers([], self.known)
        self.assertEqual(len(result), 5)
        paper = next(p for p in result if p['title'].startswith('ODesign'))
        self.assertEqual(paper['year'], 2025)
        html = updater.generate_paper_html(paper, self.known)
        self.assertIn('<strong>Yunpeng Xia</strong>', html)
        self.assertIn('https://arxiv.org/abs/2510.22304', html)

    def test_odesign_api_record_does_not_duplicate_curated_entry(self):
        for ids in ({'DOI': '10.48550/arXiv.2510.22304'}, {'ArXiv': '2510.22304'}, {}):
            result = updater.prepare_papers([{'title': 'ODesign: A World Model for Biomolecular Interaction Design',
                                              'externalIds': ids, 'citationCount': 9}], self.known)
            self.assertEqual(len(result), 5)
            paper = next(p for p in result if p['title'].startswith('ODesign'))
            self.assertEqual(paper['citationCount'], 9)
            self.assertIn('<strong>Yunpeng Xia</strong>', updater.generate_paper_html(paper, self.known))

    def test_duplicate_doi_and_title_variants(self):
        papers = [{'title': 'Some RNA–ligand Study', 'externalIds': {'DOI': '10.1234/ABC'}},
                  {'title': 'Some RNA-ligand study', 'externalIds': {'DOI': '10.1234/abc'}},
                  {'title': 'Some RNA ligand study', 'externalIds': {}}]
        self.assertEqual(len(updater.prepare_papers(papers, {})), 1)

    def test_long_titles_with_same_prefix_remain_distinct(self):
        prefix = 'A very long publication title about science ' * 2
        papers = [{'title': prefix + 'one'}, {'title': prefix + 'two'}]
        self.assertEqual(len(updater.prepare_papers(papers, {})), 2)

    def test_alias_only_still_uses_curated_details(self):
        result = updater.prepare_papers([self.preprint], self.known)
        gerna = next(p for p in result if p.get('_known_key') == 'doi:10.1038/s42256-025-01154-z')
        self.assertIn('Deciphering RNA-ligand', updater.generate_paper_html(gerna, self.known))
        self.assertEqual(len(result), 5)


if __name__ == '__main__':
    unittest.main()

import tempfile
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from easy_apply.models import Settings
from easy_apply.priority import listing_date, queue_order
from easy_apply.store import Store


class PriorityTests(unittest.TestCase):
    def test_last_day_first_then_quant_then_freshness(self):
        settings = Settings(prioritize_quant=True, newest_first=True)
        rows = [
            {'id': 'ds', 'title': 'Data Scientist', 'posted_at': '2026-10-18T12:00:00Z'},
            {'id': 'old', 'title': 'Quant Developer', 'posted_at': '2026-09-01T00:00:00Z', 'retry_priority': 1},
            {'id': 'new', 'title': 'Quantitative Analyst', 'posted_at': '2026-10-18T10:00:00Z'},
            {'id': 'unrelated', 'title': 'Quantity Surveyor', 'posted_at': '2026-10-18T11:00:00Z'},
        ]
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.assertEqual([r['id'] for r in sorted(rows, key=lambda r: queue_order(r, settings, now))], ['new', 'ds', 'unrelated', 'old'])

    def test_recent_search_evidence_expires_and_discovery_is_not_posting(self):
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        settings = Settings(newest_first=True)
        def row(seen, posted=''):
            return {'title': 'Quant', 'first_seen': now.isoformat(), 'posted_at': posted,
                    'snapshot': json.dumps({'search_listing': {'recent_search_at': seen}})}
        self.assertEqual(queue_order(row('2026-10-18T10:00:00Z'), settings, now)[0], 0)
        self.assertEqual(queue_order(row('2026-10-16T10:00:00Z'), settings, now)[0], 1)
        self.assertEqual(queue_order(row(''), settings, now)[0], 1)
        self.assertEqual(queue_order(row('2026-10-18T10:00:00Z', '2026-10-01T00:00:00Z'), settings, now)[0], 1)
        self.assertEqual(queue_order(row('', '2026-10-17T12:00:00Z'), settings, now)[0], 0)
        self.assertEqual(queue_order(row('', '2026-10-17T11:59:59Z'), settings, now)[0], 1)

    def test_listing_date_and_discovery_fallback(self):
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.assertEqual(listing_date('Reposted 2 days ago', now), '2026-10-16T12:00:00+00:00')
        self.assertEqual(listing_date('3 hours ago', now), '2026-10-18T09:00:00+00:00')
        self.assertEqual(listing_date('unavailable', now), '')
        self.assertEqual(listing_date('2099-01-01', now), '')
        settings = Settings(newest_first=True)
        rows = [{'title': 'ML Engineer', 'updated': '2026-10-18T12:00:00Z'},
                {'title': 'Data Scientist', 'first_seen': '2026-10-17T00:00:00Z'}]
        self.assertEqual(sorted(rows, key=lambda r: queue_order(r, settings))[0]['title'], 'Data Scientist')

    def test_discovery_date_is_immutable_when_requeued_or_seen_again(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            store.now = lambda: '2026-10-01T00:00:00+00:00'
            store.add('https://www.linkedin.com/jobs/view/123/')
            store.now = lambda: '2026-10-18T00:00:00+00:00'
            store.update('123', 'needs_input')
            store.requeue('123')
            store.add('https://www.linkedin.com/jobs/view/123/')
            store.save_listing_metadata('123', {'posted_text': '2 days ago'})
            initial_posted = store.rows()[0]['posted_at']
            store.save_listing_metadata('123', {'posted_text': '2 days ago'})
            row = store.rows()[0]
            self.assertEqual(row['first_seen'], '2026-10-01T00:00:00+00:00')
            self.assertEqual(row['posted_at'], initial_posted)
            self.assertTrue(initial_posted)

    def test_newest_search_parameter_is_optional(self):
        self.assertNotIn('sortBy', parse_qs(urlparse(Settings().search_url()).query))
        self.assertEqual(parse_qs(urlparse(Settings(newest_first=True).search_url()).query)['sortBy'], ['DD'])
        settings = Settings(newest_first=True)
        self.assertEqual(parse_qs(urlparse(settings.search_url(True)).query)['f_TPR'], ['r86400'])
        self.assertNotIn('f_TPR', parse_qs(urlparse(settings.search_url()).query))


if __name__ == '__main__':
    unittest.main()

import json
import queue
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from easy_apply.browser import DailyLimitReached
from easy_apply.desktop import BrowserWorker
from easy_apply.limit_report import daily_limit_report, limit_details, limit_status, save_limit_report, today_limit_report
from easy_apply.limits import DailyLimit
from easy_apply.models import Settings
from easy_apply.store import Store


class LimitReportTests(unittest.TestCase):
    def test_only_todays_limit_is_restored_with_current_queue_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            report = daily_limit_report(store, 'You reached today’s Easy Apply limit')
            report['detected_at'] = (datetime.now(ZoneInfo('Europe/London')) - timedelta(days=1)).isoformat()
            save_limit_report(store, report)
            self.assertIsNone(today_limit_report(store))
            report['detected_at'] = datetime.now(ZoneInfo('Europe/London')).isoformat()
            save_limit_report(store, report)
            store.add('https://www.linkedin.com/jobs/view/123/')
            (Path(directory) / 'reports' / 'daily-limit-zzz.json').write_text('{broken')
            restored = today_limit_report(store)
            self.assertEqual(restored['queued_remaining'], 1)
            self.assertTrue(limit_status(restored).startswith("Today's complete"))
            self.assertEqual(restored['detected_at'], report['detected_at'])

    def test_worker_reports_limit_and_preserves_queue_and_uncertain_send(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            for identifier in ('123', '456'):
                store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
            store.update('456', 'unverified', 'Verify before retry')
            messages = queue.Queue()
            worker = BrowserWorker(store, messages)
            browser = Mock()
            browser.page.is_closed.return_value = False
            browser.run.side_effect = DailyLimitReached('You reached today’s Easy Apply limit', 2, '456')
            worker.commands.put(('apply', Settings()))
            worker.commands.put(('quit', None))
            with patch('easy_apply.desktop.LinkedInBrowser', return_value=browser):
                worker.run()
            kind, report = messages.get_nowait()
            self.assertEqual(kind, 'daily_limit')
            self.assertEqual(report['confirmed_in_run'], 2)
            self.assertEqual(report['queued_remaining'], 1)
            self.assertEqual(report['unverified_job_ids'], ['456'])
            self.assertEqual(json.loads(Path(report['report_path']).read_text(encoding='utf-8'))['event'], 'linkedin_daily_limit')
            remaining = list(messages.queue)
            self.assertTrue(any(kind == 'daily_report' for kind, _ in remaining))
            self.assertEqual(remaining[-1][0], 'idle')
            self.assertIn('Confirmed applications this run: 2', limit_details(report))
            self.assertIn('daily limit reached', limit_status(report))
            self.assertEqual(store.rows('queued')[0]['id'], '123')
            self.assertEqual(store.rows('unverified')[0]['id'], '456')

    def test_scan_limit_still_notifies_when_report_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            messages = queue.Queue()
            worker = BrowserWorker(store, messages)
            browser = Mock()
            browser.page.is_closed.return_value = False
            browser.discover.side_effect = DailyLimit('You reached today’s Easy Apply limit')
            worker.commands.put(('scan', Settings()))
            worker.commands.put(('quit', None))
            with patch('easy_apply.desktop.LinkedInBrowser', return_value=browser), \
                 patch('easy_apply.desktop.save_limit_report', side_effect=OSError('Disk full')):
                worker.run()
            kind, report = messages.get_nowait()
            self.assertEqual(kind, 'daily_limit')
            self.assertIsNone(report['confirmed_in_run'])
            self.assertIn('Could not save report: Disk full', limit_details(report))
            self.assertEqual(messages.get_nowait()[0], 'idle')

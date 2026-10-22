import queue
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from easy_apply.browser import DailyLimitReached
from easy_apply.desktop import BrowserWorker
from easy_apply.models import Settings
from easy_apply.schedule import ScheduleQueue, schedule_due
from easy_apply.store import Store


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'jobs.sqlite3')
        self.schedule = ScheduleQueue(self.store)
        self.now = datetime(2026, 10, 19, 9, tzinfo=timezone(timedelta(hours=1)))

    def test_one_request_and_one_claim_per_local_day(self):
        day, created = self.schedule.request(self.now)
        self.assertTrue(created)
        self.assertFalse(self.schedule.request(self.now)[1])
        self.assertEqual(self.schedule.claim(now=self.now), day)
        self.assertIsNone(ScheduleQueue(self.store).claim(now=self.now))
        self.schedule.finish(day, 'completed', 'Run finished: 5 confirmed applications.')
        self.assertFalse(self.schedule.request(self.now)[1])
        self.assertTrue(self.schedule.request(self.now + timedelta(days=1))[1])

    def test_busy_requests_skip_without_delayed_surprise_run(self):
        day, _ = self.schedule.request(self.now)
        self.assertEqual(self.schedule.claim(busy=True, now=self.now), day)
        self.assertEqual(self.schedule.latest()['status'], 'skipped')
        self.assertIsNone(self.schedule.claim(now=self.now))

    def test_stale_requests_and_interrupted_runs_are_not_replayed(self):
        self.schedule.request(self.now - timedelta(days=1))
        self.assertIsNone(self.schedule.claim(now=self.now))
        self.assertEqual(self.schedule.latest()['status'], 'expired')
        self.schedule.request(self.now)
        self.schedule.claim(now=self.now)
        self.schedule.recover()
        self.assertEqual(self.schedule.latest()['status'], 'interrupted')
        self.assertIsNone(self.schedule.claim(now=self.now))

    def test_request_uses_local_calendar_day(self):
        local = datetime(2026, 10, 20, 0, 15, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(self.schedule.request(local)[0], '2026-10-20')

    def test_scheduler_entry_delivers_once_and_opens_or_hands_off_to_desktop(self):
        from easy_apply.scheduled import main
        with patch('easy_apply.scheduled.Store', return_value=self.store), \
             patch('easy_apply.desktop.main', side_effect=lambda **kwargs: self.schedule.claim()) as desktop:
            main()
            main()
        desktop.assert_called_once_with(quiet_if_open=True)
        self.assertEqual(self.schedule.latest()['status'], 'running')

    def test_due_guard_respects_time_disabled_setting_and_missing_config(self):
        import json
        path = self.store.path.parent / 'schedule.json'
        self.assertFalse(schedule_due(path.parent, self.now))
        path.write_text(json.dumps({'enabled':True,'time':'09:00'}))
        self.assertFalse(schedule_due(path.parent, self.now.replace(hour=8, minute=59)))
        self.assertTrue(schedule_due(path.parent, self.now))
        self.assertTrue(schedule_due(path.parent, self.now.replace(hour=13)))
        path.write_text(json.dumps({'enabled':False,'time':'09:00'}))
        self.assertFalse(schedule_due(path.parent, self.now.replace(hour=13)))

    def test_pending_request_can_relaunch_after_startup_failure(self):
        from easy_apply.scheduled import main
        with patch('easy_apply.scheduled.Store', return_value=self.store), patch('easy_apply.desktop.main') as desktop:
            desktop.side_effect = RuntimeError('startup failed')
            with self.assertRaises(RuntimeError):
                main()
            self.assertEqual(self.schedule.latest()['status'], 'pending')
            desktop.side_effect = lambda **kwargs: self.schedule.claim()
            main()
            main()
        self.assertEqual(desktop.call_count, 2)
        self.assertEqual(self.schedule.latest()['status'], 'running')

    def test_scheduled_entry_does_not_launch_before_due(self):
        from easy_apply.scheduled import main
        with patch('easy_apply.scheduled.Store',return_value=self.store), patch('easy_apply.scheduled.schedule_due',return_value=False), patch('easy_apply.desktop.main') as desktop:
            main(if_due=True)
        desktop.assert_not_called()
        self.assertIsNone(self.schedule.latest())

    def run_worker(self, browser):
        day, _ = self.schedule.request(self.now)
        self.schedule.claim(now=self.now)
        messages = queue.Queue()
        worker = BrowserWorker(self.store, messages)
        settings = Settings(keywords='quant', location='United Kingdom')
        worker.commands.put(('scheduled_apply', settings, day))
        worker.commands.put(('quit', None))
        with patch('easy_apply.desktop.LinkedInBrowser', return_value=browser):
            worker.run()
        return settings, list(messages.queue)

    def test_scheduled_run_opens_browser_and_refreshes_before_applying(self):
        browser = Mock()
        settings, messages = self.run_worker(browser)
        names = [call[0] for call in browser.method_calls]
        self.assertLess(names.index('open'), names.index('page.goto'))
        self.assertLess(names.index('page.goto'), names.index('run'))
        browser.page.goto.assert_called_once_with(settings.search_url(), wait_until='domcontentloaded')
        browser.run.assert_called_once_with(settings, self.store)
        self.assertEqual(self.schedule.latest()['status'], 'completed')
        self.assertEqual(messages[-1][0], 'idle')
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='report_email_outbox'").fetchone()
            if exists:
                self.assertEqual(db.execute('SELECT count(*) FROM report_email_outbox').fetchone()[0], 0)

    def test_failed_open_never_starts_application(self):
        browser = Mock()
        browser.open.side_effect = RuntimeError('Cannot open browser')
        self.run_worker(browser)
        browser.run.assert_not_called()
        self.assertEqual(self.schedule.latest()['status'], 'error')

    def test_scheduled_limit_is_reported_and_not_retried(self):
        browser = Mock()
        browser.run.side_effect = DailyLimitReached('You reached today’s Easy Apply limit', 0)
        _, messages = self.run_worker(browser)
        self.assertEqual(self.schedule.latest()['status'], 'daily_limit')
        self.assertTrue(next(value for kind, value in messages if kind == 'daily_limit')['scheduled'])
        browser.run.assert_called_once()
        self.assertFalse(self.schedule.request(self.now)[1])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM report_email_outbox').fetchone()[0], 1)
        self.assertTrue(any(kind == 'email_status' for kind, _ in messages))

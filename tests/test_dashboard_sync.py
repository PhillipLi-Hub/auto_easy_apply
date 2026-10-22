import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from easy_apply.store import Store
from easy_apply.daily_report import begin_run, finish_run, local_day
from easy_apply.dashboard_sync import export_report, runtime_snapshot, sync_once, valid_origin, DashboardPublisher, NoRedirect


class DashboardSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'jobs.sqlite3')
        self.auth = patch('easy_apply.dashboard_sync.credentials', return_value=('https://example.chatgpt.site','test-token'))
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_allowlist_excludes_private_answers_notes_and_unconfirmed_jobs(self):
        self.store.add('https://www.linkedin.com/jobs/view/1/')
        self.store.update('1','queued',job={'title':'Quant Analyst','company':'Example','description':'secret CV text'})
        self.store.claim('1')
        self.store.update('1','applied','secret question answer',source='app_confirmed')
        self.store.add('https://www.linkedin.com/jobs/view/2/')
        self.store.update('2','applied',source='linkedin_history')
        run=begin_run(self.store,'apply')
        finish_run(self.store,run,'completed','secret API error body')
        report=export_report(self.store,local_day())
        self.assertEqual(report['total_confirmed'],1)
        self.assertEqual(report['applications'][0]['category'],'Quant')
        self.assertNotIn('secret',json.dumps(report))
        self.assertNotIn('detail',report['runs'][0])
        self.assertNotIn('url',report['applications'][0])

    def test_failed_upload_retries_and_success_is_deduplicated_but_heartbeat_remains(self):
        sender=Mock(side_effect=OSError('offline'))
        with self.assertRaises(OSError):
            sync_once(self.store,sender=sender)
        sender=Mock()
        self.assertEqual(sync_once(self.store,sender=sender),1)
        self.assertEqual(len(sender.call_args.args[2]['reports']),1)
        sender.reset_mock()
        self.assertEqual(sync_once(self.store,sender=sender),0)
        self.assertEqual(sender.call_args.args[2]['reports'],[])
        self.assertIn('updated_at',sender.call_args.args[2]['runtime'])
        run=begin_run(self.store,'apply');finish_run(self.store,run,'daily_limit','limit')
        self.assertEqual(sync_once(self.store,sender=sender),1)
        self.assertEqual(sender.call_args.args[2]['runtime']['state'],'daily_limit')

    def test_yesterdays_completion_does_not_become_todays_status(self):
        self.store.now=lambda:'2025-01-01T10:00:00+00:00'
        run=begin_run(self.store,'apply');finish_run(self.store,run,'daily_limit','limit')
        self.assertEqual(runtime_snapshot(self.store,{'state':'daily_limit'})['state'],'idle')

    def test_rejects_credential_redirects_and_untrusted_destinations(self):
        for origin in ['http://example.chatgpt.site','https://example.com','https://example.chatgpt.site.evil.com','https://user:pass@example.chatgpt.site','https://example.chatgpt.site/?x=1']:
            with self.assertRaises(ValueError): valid_origin(origin)
        self.assertEqual(valid_origin('https://example.chatgpt.site/'),'https://example.chatgpt.site')
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'redirect',{},'https://example.com'))

    def test_background_failure_is_redacted_and_does_not_block_app(self):
        messages=queue.Queue()
        worker=DashboardPublisher(self.store,messages,interval=1)
        def fail(*args):
            worker.close()
            raise OSError('private-token and CV')
        with patch('easy_apply.dashboard_sync.load_config',return_value={'enabled':True}),patch('easy_apply.dashboard_sync.sync_once',side_effect=fail):
            worker.start();worker.join(2)
        self.assertFalse(worker.is_alive())
        kind,note=messages.get_nowait()
        self.assertEqual(kind,'dashboard_status')
        self.assertIn('retry',note)
        self.assertNotIn('private-token',note)

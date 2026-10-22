import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from easy_apply.daily_report import role_category, local_day, build_report, save_report, begin_run, finish_run, render_html, application_totals
from easy_apply.store import Store


class DailyReportTests(unittest.TestCase):
    def test_header_counts_confirmed_ledger_and_rolls_over_at_london_midnight(self):
        self.store.now = lambda: '2026-10-21T22:59:00+00:00'
        self.confirm('1', 'Quant Analyst')
        self.store.now = lambda: '2026-10-21T23:01:00+00:00'
        self.confirm('2', 'Data Scientist')
        self.confirm('3', 'Trader', 'linkedin_history')
        self.confirm('4', 'Engineer', 'manual_verified')
        self.store.add('https://www.linkedin.com/jobs/view/5/')
        self.store.update('5', 'unverified')
        for day, expected in [('2026-10-22', (1, 2)), ('2026-10-23', (0, 2))]:
            with patch('easy_apply.daily_report.local_day', side_effect=lambda stamp=None: local_day(stamp) if stamp else day):
                self.assertEqual(application_totals(self.store), expected)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'jobs.sqlite3')
        self.store.now = lambda: '2026-10-18T10:00:00+00:00'

    def confirm(self, identifier, title, source='app_confirmed'):
        self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
        self.store.update(identifier, 'queued', job={'title': title, 'company': 'Example Ltd'})
        self.store.claim(identifier)
        self.store.update(identifier, 'applied', source=source)

    def test_categories_are_mutually_exclusive_and_cover_requested_roles(self):
        cases = {'Quantitative Developer': 'Quant', 'Quant Trader': 'Quant', 'Data Scientist': 'Data Scientist',
                 'Software Engineer': 'Software Engineer', 'Python Developer': 'Software Engineer',
                 'Trader': 'Trader', 'Machine Learning Engineer': 'Machine Learning Engineer',
                 'AI Engineer': 'AI Engineer', 'LLM Engineer': 'AI Engineer', 'Credit Risk Analyst': 'Risk',
                 'Risk Data Scientist': 'Risk', 'AI Data Scientist': 'Data Scientist',
                 'ML / AI Engineer': 'Machine Learning Engineer', 'Quantity Surveyor': 'Other',
                 '': 'Other', 'Applied Scientist': 'Other'}
        for title, category in cases.items():
            with self.subTest(title=title):
                self.assertEqual(role_category(title), category)

    def test_daily_totals_exclude_history_manual_and_unverified(self):
        self.confirm('1', 'Quantitative Analyst')
        self.confirm('2', 'Data Scientist')
        self.confirm('3', 'Trader', 'linkedin_history')
        self.confirm('4', 'AI Engineer', 'manual_verified')
        self.store.add('https://www.linkedin.com/jobs/view/5/')
        self.store.claim('5')
        self.store.update('5', 'unverified')
        report = build_report(self.store, '2026-10-18')
        self.assertEqual(report['total_confirmed'], 2)
        self.assertEqual(sum(report['counts'].values()), 2)
        self.assertEqual(report['counts']['Quant'], 1)
        self.assertEqual(report['counts']['Data Scientist'], 1)
        self.assertEqual(report['counts']['Trader'], 0)
        self.assertEqual(report['unverified_current'], 1)

    def test_confirmation_is_immutable_across_rescans_and_reverification(self):
        self.confirm('1', 'Quant Analyst')
        self.store.now = lambda: '2026-10-19T10:00:00+00:00'
        self.store.update('1', 'applied', source='app_confirmed')
        self.store.update('1', 'applied', source='linkedin_history')
        self.store.save_listing_metadata('1', {'title': 'Quant Analyst'})
        self.assertEqual(build_report(self.store, '2026-10-18')['total_confirmed'], 1)
        self.assertEqual(build_report(self.store, '2026-10-19')['total_confirmed'], 0)

    def test_london_calendar_day_and_midnight_verification(self):
        self.assertEqual(local_day('2026-07-01T23:30:00Z'), '2026-07-02')
        self.assertEqual(local_day('2026-12-01T23:30:00Z'), '2026-12-01')
        self.store.now = lambda: '2026-07-01T22:59:50+00:00'
        self.store.add('https://www.linkedin.com/jobs/view/1/')
        self.store.claim('1')
        self.store.now = lambda: '2026-07-01T23:00:10+00:00'
        self.store.update('1', 'applied', source='app_confirmed')
        self.assertEqual(build_report(self.store, '2026-07-01')['total_confirmed'], 1)
        self.assertEqual(build_report(self.store, '2026-07-02')['total_confirmed'], 0)

    def test_legacy_dates_are_labelled_approximate_and_imported_once(self):
        self.confirm('1', 'Risk Analyst')
        with self.store.connect() as db:
            db.execute('DELETE FROM confirmed_applications')
        migrated = Store(self.store.path)
        report = build_report(migrated, '2026-10-18')
        self.assertEqual(report['approximate_date_count'], 1)
        self.assertIn('Historical dates are approximate', render_html(report))
        with migrated.connect() as db:
            db.execute("UPDATE jobs SET updated='2026-10-19T10:00:00Z'")
        migrated = Store(self.store.path)
        self.assertEqual(build_report(migrated, '2026-10-18')['total_confirmed'], 1)

    def test_saved_files_refresh_cumulatively_and_escape_role_text(self):
        self.confirm('1', '<script>alert(1)</script> Quant Analyst')
        report, path = save_report(self.store, '2026-10-18')
        self.assertNotIn('<script>', path.read_text(encoding='utf-8'))
        self.assertIn('&lt;script&gt;', path.read_text(encoding='utf-8'))
        self.confirm('2', 'Data Scientist')
        report, same_path = save_report(self.store, '2026-10-18')
        self.assertEqual(path, same_path)
        self.assertEqual(report['total_confirmed'], 2)
        self.assertEqual(json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))['total_confirmed'], 2)
        self.assertIn('Total,2', path.with_suffix('.csv').read_text(encoding='utf-8-sig'))

    def test_zero_day_and_run_outcomes_across_midnight(self):
        self.store.now = lambda: '2026-07-01T22:59:00+00:00'
        run = begin_run(self.store, 'scheduled_apply', '2026-07-01')
        self.store.now = lambda: '2026-07-01T23:01:00+00:00'
        days = finish_run(self.store, run, 'daily_limit', 'Daily maximum reached')
        self.assertEqual(days, ['2026-07-01', '2026-07-02'])
        report = build_report(self.store, days[-1])
        self.assertEqual(report['total_confirmed'], 0)
        self.assertEqual(report['runs'][0]['outcome'], 'daily_limit')
        self.assertIn('No confirmed applications recorded', render_html(report))

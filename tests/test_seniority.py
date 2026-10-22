import tempfile
import unittest
from pathlib import Path

from easy_apply.models import Settings, load_settings, save_settings
from easy_apply.store import Store


class SeniorityTests(unittest.TestCase):
    def test_role_titles_and_abbreviations(self):
        settings = Settings(exclude_senior_roles=True, allow_missing_metadata=True)
        for title in ('Senior Quant Analyst', 'Sr. Data Scientist', 'Quant Analyst (SR)',
                      'Director of AI', 'Associate Director, Risk', 'Portfolio Manager',
                      'ML Engineering Mgr.', 'Senior/Junior Quant Developer',
                      'Principal Quantitative Researcher', 'Principle Data Scientist',
                      'Staff Machine Learning Engineer', 'STAFF AI Engineer',
                      'Head of Machine Learning', 'Global Head of Quant Research',
                      'Co-Head of Trading', 'ML Engineering Head', 'Head Quant Researcher',
                      'Lead Data Scientist', 'Quant Research Team Leader', 'Tech Lead',
                      'Chief Risk Officer', 'Vice President - Quant', 'VP of AI',
                      'SVP Engineering', 'CTO', 'CEO / Founder'):
            with self.subTest(title=title):
                self.assertIn('Excluded role level', settings.rejection({'title': title}))
        self.assertIn('missing title', settings.rejection({}))

    def test_description_and_partial_words_do_not_exclude(self):
        settings = Settings(exclude_senior_roles=True)
        for title in ('Quantitative Analyst', 'Data Scientist', 'Trading Systems Engineer',
                      'Directory Services Engineer', 'Risk Management Analyst',
                      'Data Scientist - Stafford', 'Quant Analyst - Staffing Analytics',
                      'Analyst - Head Office', 'Headcount Analyst', 'Lead Generation Analyst',
                      'Lead Qualification Analyst', 'Developer - Headway', 'Leadership Analyst'):
            self.assertIsNone(settings.rejection({'title': title,
                'description': 'Report to the Head of ML, Senior Manager, Tech Lead and Director.',
                'company': 'Head of AI Staffing'}))
        self.assertIsNone(Settings().rejection({'title': 'Senior Quant'}))

    def test_persistence_and_pending_queue_only(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(exclude_senior_roles=True)
            path = Path(directory) / 'settings.json'
            save_settings(settings, path)
            self.assertTrue(load_settings(path).exclude_senior_roles)
            store = Store(Path(directory) / 'jobs.sqlite3')
            states = ['queued', 'needs_input', 'error', 'applied', 'unverified', 'submitting']
            for i, state in enumerate(states):
                store.add(f'https://www.linkedin.com/jobs/view/{i}/')
                store.update(str(i), state, job={'title': 'Senior Quant Analyst'})
            store.add('https://www.linkedin.com/jobs/view/100/')
            store.update('100', 'queued', job={'title': 'Quant Analyst'})
            self.assertEqual(store.exclude_ineligible_jobs(settings), 3)
            rows = {row['id']: row for row in store.rows()}
            for i, state in enumerate(states):
                self.assertEqual(rows[str(i)]['status'], 'skipped' if i < 3 else state)
            self.assertEqual(rows['100']['status'], 'queued')
            self.assertEqual(store.exclude_ineligible_jobs(settings), 0)

    def test_principal_and_staff_queue_cleanup_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            titles = ['Principal Quant', 'Principle Data Scientist', 'Staff ML Engineer',
                      'Head of Machine Learning', 'Lead Quant', 'Chief Scientist', 'VP of AI']
            for i, title in enumerate(titles):
                for offset, state in enumerate(('queued', 'needs_input', 'applied', 'unverified', 'submitting')):
                    identifier = str(i * 10 + offset)
                    store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
                    store.update(identifier, state, job={'title': title})
            self.assertEqual(store.exclude_ineligible_jobs(Settings(exclude_senior_roles=True)), len(titles) * 2)
            rows = {row['id']: row for row in store.rows()}
            for i in range(len(titles)):
                for offset, state in enumerate(('skipped', 'skipped', 'applied', 'unverified', 'submitting')):
                    self.assertEqual(rows[str(i * 10 + offset)]['status'], state)

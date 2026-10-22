import tempfile
import unittest
from pathlib import Path

from easy_apply.clearance import requires_sc, REASON
from easy_apply.models import Settings, load_settings, save_settings
from easy_apply.store import Store


class ClearanceTests(unittest.TestCase):
    def test_explicit_requirements(self):
        for text in ('SC clearance required', 'You must hold active SC clearance.',
                     'Current SC clearance is essential.', 'Must be SC-cleared',
                     'Security Check (SC) clearance is mandatory.',
                     'Applicants are required to have valid SC clearance.',
                     'SC/DV clearance required', 'We are looking for an SC-cleared data scientist.'):
            with self.subTest(text=text):
                self.assertTrue(requires_sc(description=text))
        self.assertTrue(requires_sc(title='Data Scientist - SC Cleared'))

    def test_optional_clearance_and_screening_are_not_requirements(self):
        for text in ('Do you hold active SC Clearance or higher?',
                     'SC clearance is desirable.', 'SC clearance is not required.',
                     'No SC clearance required', 'Must be eligible for SC clearance.',
                     'Must be willing to obtain SC clearance.', 'SC clearance preferred',
                     'MSc required in data science.', 'Must have Python experience.'):
            with self.subTest(text=text):
                self.assertFalse(requires_sc(description=text))
        self.assertTrue(requires_sc(description='DV clearance is desirable. SC clearance is required.'))

    def test_filter_is_persistent_and_independent_of_missing_metadata(self):
        settings = Settings(exclude_sc_required=True, allow_missing_metadata=True)
        self.assertEqual(settings.rejection({'title': 'Quant Developer - SC Cleared'}), REASON)
        self.assertIsNone(Settings().rejection({'title': 'Quant Developer - SC Cleared'}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            save_settings(settings, path)
            self.assertTrue(load_settings(path).exclude_sc_required)

    def test_prune_pending_jobs_preserves_history_and_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            states = ['queued', 'needs_input', 'error', 'applied', 'submitting', 'unverified']
            for i, state in enumerate(states):
                store.add(f'https://www.linkedin.com/jobs/view/{i}/')
                store.update(str(i), state, job={'title': 'Data Scientist', 'description': 'SC clearance required'})
            store.add('https://www.linkedin.com/jobs/view/100/')
            store.save_listing_metadata('100', {'title': 'Quant Analyst - SC Cleared'})
            store.add('https://www.linkedin.com/jobs/view/101/')
            store.update('101', 'needs_input', 'Do you hold SC clearance?')
            self.assertEqual(store.exclude_clearance_jobs(Settings()), 0)
            self.assertEqual(store.exclude_clearance_jobs(Settings(exclude_sc_required=True)), 4)
            rows = {row['id']: row for row in store.rows()}
            for i, state in enumerate(states):
                self.assertEqual(rows[str(i)]['status'], 'skipped' if i < 3 else state)
                self.assertIn('SC clearance required', rows[str(i)]['snapshot'])
            self.assertEqual(rows['101']['status'], 'needs_input')
            self.assertEqual(store.exclude_clearance_jobs(Settings(exclude_sc_required=True)), 0)

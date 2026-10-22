import tempfile
import unittest
from pathlib import Path

from easy_apply.employment import employment_rejection
from easy_apply.models import Settings, load_settings, save_settings
from easy_apply.store import Store


class EmploymentTests(unittest.TestCase):
    def test_rejects_nonpermanent_roles_and_rates(self):
        jobs = [
            {'title': title} for title in ('Quant Analyst - Contract', 'ML Engineer (6 month FTC)',
                'Interim Data Scientist', 'Freelance ML Engineer', 'Part-time Quant', 'Quant Internship')
        ] + [
            {'employment_type': 'Full-time / Contract'}, {'employment_type': ['FULL_TIME', 'TEMPORARY']},
            {'salary': '500 GBP/day'}, {'salary': '£500 - £700 per day'}, {'salary': '£80/hour'},
        ]
        jobs.extend({'description': text} for text in (
            'This is a 12-month contract.', 'Employment type: Contract', 'Contract type: fixed-term',
            'Contract length: 6 months (extension likely)',
            'This role is temporary.', 'We offer a freelance position.', 'Full-time, outside IR35.',
            'Day rate: negotiable', '600pd', 'Contract-to-hire role', 'Maternity cover',
            'Permanent position in future; initial 6 month contract.', 'Type: Full-time / Contract'))
        for job in jobs:
            with self.subTest(job=job):
                self.assertIsNotNone(employment_rejection(job))

    def test_retains_permanent_roles_with_contract_related_skills(self):
        for job in ({'title': 'Smart Contract Engineer'}, {'title': 'Futures Contracts Quantitative Analyst'},
                    {'employment_type': 'Full-time / Permanent', 'salary': '£75,000 - £95,000 per year'},
                    {'description': 'Permanent employment contract. Contract negotiation experience is desirable.'},
                    {'description': 'You will work with contractors and price futures contracts.'},
                    {'description': 'Five days per week, eight hours per day. Salary: £85k per annum.'},
                    {'description': 'Permanent role, not a contract role.'}):
            with self.subTest(job=job):
                self.assertIsNone(employment_rejection(job))

    def test_setting_persists_and_rejects_even_with_missing_metadata_allowed(self):
        settings = Settings(permanent_full_time_only=True, allow_missing_metadata=True)
        self.assertIsNotNone(settings.rejection({'employment_type': 'Contract'}))
        self.assertIsNone(Settings().rejection({'employment_type': 'Contract'}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            save_settings(settings, path)
            self.assertTrue(load_settings(path).permanent_full_time_only)

    def test_pruning_preserves_submission_history_and_handles_listing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'jobs.sqlite3')
            states = ['queued', 'needs_input', 'error', 'applied', 'unverified', 'submitting']
            for i, state in enumerate(states):
                store.add(f'https://www.linkedin.com/jobs/view/{i}/')
                store.update(str(i), state, job={'title': 'Quant Analyst', 'employment_type': 'Contract'})
            store.add('https://www.linkedin.com/jobs/view/100/')
            store.save_listing_metadata('100', {'title': 'Data Scientist', 'salary': '£600/day'})
            store.add('https://www.linkedin.com/jobs/view/101/')
            store.update('101', 'queued', job={'title': 'Quant Developer', 'employment_type': 'Full-time'})
            self.assertEqual(store.exclude_ineligible_jobs(Settings()), 0)
            settings = Settings(permanent_full_time_only=True)
            self.assertEqual(store.exclude_ineligible_jobs(settings), 4)
            rows = {row['id']: row for row in store.rows()}
            for i, state in enumerate(states):
                self.assertEqual(rows[str(i)]['status'], 'skipped' if i < 3 else state)
            self.assertEqual(rows['101']['status'], 'queued')
            self.assertEqual(store.exclude_ineligible_jobs(settings), 0)

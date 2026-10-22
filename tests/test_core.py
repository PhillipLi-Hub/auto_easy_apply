import tempfile
import unittest
from pathlib import Path

from easy_apply.models import Settings, job_id, load_settings, save_settings
from easy_apply.store import Store, queue_summary, status_label


class SettingsTests(unittest.TestCase):
    def test_queue_summary_separates_all_searches_from_matching_titles(self):
        rows = [{'status': 'queued', 'title': 'Data Scientist'},
                {'status': 'queued', 'title': 'Quantitative Analyst'},
                {'status': 'applied', 'title': 'Quantitative Researcher', 'application_source': 'app_confirmed'}]
        summary = queue_summary(rows, Settings(title_include='quantitative'))
        self.assertIn('Queued (all searches): 2', summary)
        self.assertIn('Queued title matches: 1', summary)

    def test_phd_fact_handles_equivalent_questions_but_not_other_degrees(self):
        settings = Settings(answers={'Do you have a PhD?': 'No'})
        for label in ['Have you completed the following level of education: Doctor of Philosophy?*',
                      'Do you hold a Ph.D. degree?', 'Have you earned a PhD?',
                      'Do you possess a Doctor of Philosophy?']:
            self.assertEqual(settings.answer(label), 'No', label)
            self.assertIsNone(Settings().answer(label), label)
        for label in ['Do you have a PhD or Masters?', 'Are you a PhD candidate?',
                      'Are you willing to obtain a PhD?', 'Do you have a medical doctorate?',
                      'Do you have a Masters degree?']:
            self.assertIsNone(settings.answer(label), label)
        settings.answers['Do you hold a Ph.D. degree?'] = 'Yes'
        self.assertEqual(settings.answer('Do you hold a Ph.D. degree?'), 'Yes')

    def test_title_filter_cannot_be_bypassed_by_missing_metadata(self):
        settings = Settings(title_include='quantitative, quant researcher', allow_missing_metadata=True)
        self.assertIn('missing title', settings.rejection({}))
        self.assertIsNone(settings.rejection({'title':'Quantitative Researcher'}))
        self.assertIsNotNone(settings.rejection({'title':'Data Scientist'}))

    def test_relocation_preference_handles_places_without_inventing_conditions(self):
        settings = Settings(willing_relocate_anywhere=True)
        for label in ['Are you willing to relocate to Edinburgh?*', 'Would you be open to relocation to Germany?',
                      'Would you consider relocating to London?', 'Willing to relocate']:
            self.assertEqual(settings.answer(label), 'Yes', label)
        self.assertEqual(settings.answer('Where are you willing to relocate?'), 'I am happy to relocate anywhere.')
        for label in ['When can you relocate?', 'Are you willing to relocate within 2 weeks?',
                      'Are you willing to relocate at your own expense?', 'Are you currently based in Germany?',
                      'Are you willing to relocate without visa sponsorship?', 'Are you willing to relocate and work weekends?']:
            self.assertIsNone(settings.answer(label), label)
        self.assertIsNone(Settings().answer('Are you willing to relocate?'))
        settings.answers['Are you willing to relocate to Edinburgh?'] = 'No'
        self.assertEqual(settings.answer('Are you willing to relocate to Edinburgh?*'), 'No')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            save_settings(settings, path)
            self.assertTrue(load_settings(path).willing_relocate_anywhere)

    def test_exact_answers_no_eligibility_guessing(self):
        settings = Settings(first_name="Ada", answers={"Do you need sponsorship?": "Yes"})
        self.assertEqual(settings.answer("First name *"), "Ada")
        self.assertEqual(settings.answer("Do you need sponsorship? *"), "Yes")
        self.assertIsNone(settings.answer("Are you authorized to work in the UK?"))
        self.assertIsNone(settings.answer("How many years of Python experience?"))

    def test_filters_require_metadata_and_all_conditions(self):
        settings = Settings(location="London", title_include="developer,engineer", title_exclude="senior", company_exclude="Excluded", description_require="Python,SQL", remote_only=True)
        job = dict(title="Software Engineer", company="Example", location="London, England", description="Python and SQL", workplace="Remote")
        self.assertIsNone(settings.rejection(job))
        for change in [dict(title="Senior Engineer"), dict(company="Excluded Ltd"), dict(location="Paris"), dict(description="Python"), dict(workplace=""), dict(company="")]:
            self.assertIsNotNone(settings.rejection(job | change))

    def test_validation_and_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(first_name="Ada", last_name="Example", email="ada@example.com", keywords="Python", location="London")
            settings.validate(for_run=True)
            save_settings(settings, path)
            self.assertEqual(load_settings(path), settings)
        for settings in [Settings(max_applications=0), Settings(max_results=101), Settings(answers={"Consent": True}), Settings(answers={"X?": "one", "x": "two"})]:
            with self.assertRaises(ValueError):
                settings.validate()

    def test_job_urls_are_restricted_and_canonicalized(self):
        self.assertEqual(job_id("https://www.linkedin.com/jobs/view/developer-at-company-12345/?trackingId=test"), "12345")
        for url in ["https://evil.test/jobs/view/123/", "https://www.linkedin.com.evil.test/jobs/view/123/", "http://linkedin.com/jobs/view/123/", "https://linkedin.com/jobs/search/"]:
            with self.assertRaises(ValueError):
                job_id(url)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "jobs.sqlite3")
        self.url = "https://www.linkedin.com/jobs/view/123/"
        self.store.add(self.url)

    def test_dedup_preserves_status(self):
        self.store.update("123", "applied")
        self.assertFalse(self.store.add(self.url + "?trackingId=new"))
        self.assertEqual(self.store.rows()[0]["status"], "applied")

    def test_claim_is_atomic_and_recovery_prevents_duplicate_submit(self):
        self.assertTrue(self.store.claim("123"))
        self.assertFalse(self.store.claim("123"))
        self.store.recover()
        self.assertEqual(self.store.rows()[0]["status"], "unverified")
        self.assertFalse(self.store.requeue("123"))
        self.assertFalse(self.store.claim("123"))

    def test_requeue_needs_input(self):
        self.store.update("123", "needs_input", "Question missing")
        self.assertTrue(self.store.requeue("123"))
        self.assertEqual(self.store.rows()[0]["status"], "queued")

    def test_retries_run_before_older_ordinary_queue_items(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.store.update('456', 'needs_input', 'Missing answer')
        self.assertTrue(self.store.requeue('456'))
        self.assertEqual([row['id'] for row in self.store.rows('queued')], ['456', '123'])
        self.store.update('456', 'applied', source='app_confirmed')
        self.assertFalse(self.store.requeue('456'))
        self.assertEqual([row['id'] for row in self.store.rows('queued')], ['123'])

    def test_listing_metadata_does_not_reset_application_status(self):
        self.store.update('123', 'unverified', 'Verify submission', {'description': 'Full description'})
        self.store.save_listing_metadata('123', {'title': 'Data Scientist', 'company': 'Example', 'location': 'London'})
        row = self.store.rows()[0]
        self.assertEqual((row['title'], row['company']), ('Data Scientist', 'Example'))
        self.assertEqual((row['status'], row['note']), ('unverified', 'Verify submission'))
        self.assertIn('Full description', row['snapshot'])
        self.store.save_listing_metadata('123', {'title': '', 'company': ''})
        self.assertEqual(self.store.rows()[0]['title'], 'Data Scientist')

    def test_existing_records_backfill_only_recorded_application_origin(self):
        notes = {'123': 'LinkedIn displayed an application-sent confirmation',
                 '124': 'Search result shows already applied: Applied',
                 '125': 'User marked applied after manual verification',
                 '126': 'Unknown old note'}
        for identifier, note in notes.items():
            self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
            self.store.update(identifier, 'applied', note)
        migrated = Store(self.store.path)
        rows = {r['id']: r for r in migrated.rows()}
        self.assertEqual([rows[i]['application_source'] for i in notes],
                         ['app_confirmed', 'linkedin_history', 'manual_verified', ''])
        summary = queue_summary(list(rows.values()))
        self.assertIn('Submitted by app: 1', summary)
        self.assertIn('Already applied on LinkedIn: 1', summary)
        self.assertIn('Applied, source unknown: 1', summary)
        self.assertEqual(status_label(rows['124']), 'already applied')
        self.assertFalse(migrated.requeue('124'))

    def test_application_source_survives_metadata_refresh_and_reopening(self):
        self.store.update('123', 'applied', 'Confirmed send', source='app_confirmed')
        self.store.save_listing_metadata('123', {'title': 'Python Engineer', 'applied': 'Applied'})
        self.assertEqual(Store(self.store.path).rows()[0]['application_source'], 'app_confirmed')

    def test_old_database_schema_is_upgraded(self):
        import sqlite3
        path = Path(self.temp.name) / 'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE jobs(id TEXT PRIMARY KEY,url TEXT,title TEXT,company TEXT,status TEXT,note TEXT,updated TEXT,snapshot TEXT)""")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)", ('1', 'https://www.linkedin.com/jobs/view/1/', 'Job', 'Company', 'applied', 'Already applied on LinkedIn: Applied. No new submission.', '2026-10-12', '{}'))
        db.close()
        upgraded = Store(path)
        self.assertEqual(upgraded.rows()[0]['application_source'], 'linkedin_history')


if __name__ == "__main__":
    unittest.main()

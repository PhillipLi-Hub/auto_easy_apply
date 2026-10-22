import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from playwright.sync_api import sync_playwright

from easy_apply.browser import LinkedInBrowser, NeedsInput, Stopped, fill_form
from easy_apply.models import Settings
from easy_apply.store import Store
from easy_apply.navigation import NavigationError, prepare_action, STEP_STATE, wait_for_step_change, wait_for_form_ready, easy_apply_buttons
from easy_apply.page_data import APPLIED_STATUS, CLOSED_STATUS
from easy_apply.limits import visible_limit


JOB = """<!doctype html><html><body><main>
<h1>Python Engineer</h1><div class="job-details-jobs-unified-top-card__company-name">Example Ltd</div>
<div class="job-details-jobs-unified-top-card__primary-description-container">London, England</div>
<div class="job-details-jobs-unified-top-card__job-insight">Remote</div>
<div id="job-details">Python and SQL engineering role.</div>
<button id="apply" onclick="document.querySelector('[role=dialog]').hidden=false">Easy Apply</button>
<section role="dialog" hidden><h2>Apply to Example Ltd</h2>
<form id="fields" onsubmit="event.preventDefault();document.querySelector('#fields').hidden=true;document.querySelector('#review').hidden=false">
<label for="email">Email address *</label><input id="email" type="email" required>
<fieldset><legend>Do you need sponsorship? *</legend>
<label><input name="sponsor" type="radio" value="yes" required>Yes</label>
<label><input name="sponsor" type="radio" value="no" required>No</label></fieldset>
<button type="submit">Next</button></form>
<div id="review" hidden><p>Review your application</p>
<button onclick="window.sends=(window.sends||0)+1;document.querySelector('[role=dialog]').remove();document.body.insertAdjacentHTML('beforeend','<h2>Application sent</h2>')">Submit application</button></div>
</section></main></body></html>"""


class BrowserTests(unittest.TestCase):
    def test_known_head_title_skips_without_browser_navigation(self):
        self.settings.exclude_senior_roles = True
        self.store.update('123', 'error', job={'title': 'Head of Machine Learning'})
        with patch.object(self.driver, 'read_job') as read:
            self.assertFalse(self.driver.apply(self.store.rows()[0], self.settings, self.store))
            read.assert_not_called()
        self.assertEqual(self.store.rows()[0]['status'], 'skipped')

    def test_live_head_title_skips_and_continues_to_eligible_role(self):
        self.settings.exclude_senior_roles = True
        self.settings.max_applications = 1
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.context.unroute('**/*')
        def respond(route):
            body = JOB.replace('Python Engineer', 'Head of Machine Learning') if '/123/' in route.request.url else JOB
            route.fulfill(content_type='text/html', body=body)
        self.context.route('**/*', respond)
        self.driver.run(self.settings, self.store)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'skipped')
        self.assertEqual(rows['456']['status'], 'applied')
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_browser_timeout_records_action_without_sensitive_call_log(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        with patch.object(self.page, 'goto', side_effect=BrowserTimeout('Page.goto: Timeout 30000ms exceeded.\nprivate call details')):
            self.driver.run(self.settings, self.store)
        row = self.store.rows()[0]
        self.assertEqual(row['status'], 'error')
        self.assertIn('loading the job page (Page.goto)', row['note'])
        self.assertNotIn('private call details', row['note'])

    def test_daily_limit_mode_continues_past_local_submission_cap(self):
        from easy_apply.browser import DailyLimitReached
        self.settings.run_until_daily_limit = True
        self.settings.max_applications = 1
        for identifier in ('456', '789'):
            self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
        self.context.unroute('**/*')
        def respond(route):
            body = '<div role="dialog"><h2>You reached today’s Easy Apply limit</h2></div>' if '/789/' in route.request.url else JOB
            route.fulfill(content_type='text/html; charset=utf-8', body=body)
        self.context.route('**/*', respond)
        with self.assertRaises(DailyLimitReached) as caught:
            self.driver.run(self.settings, self.store)
        self.assertEqual(caught.exception.confirmed_in_run, 2)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'applied')
        self.assertEqual(rows['456']['status'], 'applied')
        self.assertEqual(rows['789']['status'], 'queued')

    def test_missing_button_and_navigation_timeout_continue_to_next_job(self):
        from easy_apply.browser import JobBlocked
        from playwright.sync_api import TimeoutError as BrowserTimeout
        for identifier in ('456', '789'):
            self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
        self.settings.max_applications = 1
        original = self.driver.apply
        def apply(row, settings, store):
            if row['id'] == '123':
                raise JobBlocked('Easy Apply is disabled or covered')
            if row['id'] == '456':
                self.driver.operation = 'loading the job page'
                raise BrowserTimeout('Page.goto: Timeout')
            return original(row, settings, store)
        self.driver.apply = apply
        self.driver.run(self.settings, self.store)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'needs_input')
        self.assertEqual(rows['456']['status'], 'error')
        self.assertEqual(rows['789']['status'], 'applied')

    def test_daily_limit_mode_keeps_waiting_after_two_temporary_pauses(self):
        from easy_apply.browser import DailyLimitReached
        from easy_apply.limits import DailyLimit
        self.settings.run_until_daily_limit = True
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<div role="alert">You are applying too fast</div>'))
        self.driver.pause_for_limit = Mock(side_effect=[None, None, DailyLimit('You reached today’s Easy Apply limit')])
        with self.assertRaises(DailyLimitReached):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.driver.pause_for_limit.call_count, 3)
        self.assertTrue(all(call.args[0].seconds == 600 for call in self.driver.pause_for_limit.call_args_list))
        self.assertEqual(self.store.rows()[0]['status'], 'queued')

    def test_same_job_easy_apply_link_opens_daily_limit_and_keeps_queue(self):
        from easy_apply.browser import DailyLimitReached
        content = JOB.replace(
            '<button id="apply" onclick="document.querySelector(\'[role=dialog]\').hidden=false">Easy Apply</button>',
            '''<a id="apply" aria-label="Easy Apply to this job" href="/jobs/view/123/?trackingId=test"
              onclick="event.preventDefault();document.body.insertAdjacentHTML('beforeend',
              '&lt;section role=alertdialog&gt;&lt;div role=heading&gt;You reached today’s Easy Apply limit&lt;/div&gt;&lt;button&gt;Got it&lt;/button&gt;&lt;/section&gt;')">Easy Apply</a>''')
        self.page.route('**/*', lambda route: route.fulfill(body=content, content_type='text/html; charset=utf-8'))
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.settings.newest_first = self.settings.prioritize_quant = False
        self.driver.pause_for_limit = Mock()
        with self.assertRaises(DailyLimitReached) as caught:
            self.driver.run(self.settings, self.store)
        self.assertIn('today’s Easy Apply limit', caught.exception.linkedin_message)
        self.assertEqual({row['status'] for row in self.store.rows()}, {'queued'})
        self.assertEqual(self.page.url, 'https://www.linkedin.com/jobs/view/123/')
        self.assertIsNone(self.page.evaluate('window.sends'))
        self.driver.pause_for_limit.assert_not_called()

    def test_easy_apply_link_requires_current_posting_and_ignores_recommendations(self):
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.page.set_content('''<a href="https://example.org/jobs/view/123/">Easy Apply</a>
          <a href="/jobs/view/456/">Easy Apply</a><a href="/jobs/search/">Easy Apply</a>
          <a role="button" href="javascript:alert('wrong')">Easy Apply</a>
          <aside><a href="/jobs/view/123/">Easy Apply</a></aside>
          <div id="job-details"><a href="/jobs/view/123/">Easy Apply</a></div>
          <a id="correct" href="/jobs/view/123/?trackingId=test">Easy Apply</a>''')
        entries = easy_apply_buttons(self.page)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get_attribute('id'), 'correct')

    def test_daily_limit_portal_heading_is_detected_but_sdui_description_is_not(self):
        from easy_apply.limits import DailyLimit
        self.page.set_content('''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
          <section aria-label="Primary content"><h1>Analyst</h1><h2>About the job</h2>
          <div role="heading">You reached today’s Easy Apply limit</div></section></div>
          <div hidden role="alertdialog">You reached today’s Easy Apply limit</div>''')
        self.assertIsNone(visible_limit(self.page))
        self.page.evaluate("""document.body.insertAdjacentHTML('beforeend',
          '<div><div role="heading">You reached today’s Easy Apply limit</div><button>Got it</button></div>')""")
        self.assertIsInstance(visible_limit(self.page), DailyLimit)

    def test_easy_apply_visible_text_survives_different_accessible_name(self):
        content = JOB.replace('id="apply"', 'id="apply" data-view-name="job-apply-button" aria-label="Apply to this job"')
        self.page.route('**/*', lambda route: route.fulfill(body=content))
        self.assertTrue(self.driver.apply(self.store.rows()[0], self.settings, self.store))
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_current_sdui_easy_apply_marker_and_label(self):
        self.page.set_content('''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
          <section aria-label="Primary content"><button data-view-name="job-apply-button" aria-label="Easy Apply to this job"><svg><title>LinkedIn</title></svg>Easy Apply</button>
          <h2>About the job</h2><button>Easy Apply</button>
          <h2>Recommended jobs</h2><button>Easy Apply</button></section></div>''')
        buttons = easy_apply_buttons(self.page)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get_attribute('data-view-name'), 'job-apply-button')

    def test_entry_waits_for_replacement_after_metadata_loaded(self):
        job = self.driver.read_job(self.store.rows()[0]['url'])
        self.page.evaluate('''() => {
          const old = document.querySelector('#apply'); const copy = old.cloneNode(true);
          old.remove(); setTimeout(() => document.querySelector('main').prepend(copy), 600);
        }''')
        button = self.driver.wait_for_easy_apply('123', job, self.store, timeout=3)
        self.assertEqual(button.get_attribute('id'), 'apply')
        self.assertFalse(self.page.locator('[role=dialog]').is_visible())
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_entry_locator_cannot_shift_to_another_button_after_rerender(self):
        self.page.set_content('<button id="entry">Easy Apply</button><button id="other">Delete</button>')
        entry = easy_apply_buttons(self.page)[0]
        self.page.locator('#entry').evaluate('el => el.remove()')
        self.assertEqual(entry.count(), 0)
        self.assertEqual(easy_apply_buttons(self.page), [])

    def test_easy_apply_does_not_use_description_recommendations_or_external_apply(self):
        self.page.set_content('''<div id="job-details"><button>Easy Apply</button></div>
          <aside><button>Easy Apply</button></aside><div class="job-card-container"><button>Easy Apply</button></div>
          <button hidden>Easy Apply</button><button data-view-name="job-apply-button">Apply</button>
          <a href="https://example.org">Easy Apply</a><button>Learn about Easy Apply</button>''')
        self.assertEqual(easy_apply_buttons(self.page), [])

    def test_entry_wait_recognizes_late_applied_status(self):
        job = self.driver.read_job(self.store.rows()[0]['url'])
        self.page.evaluate('''() => {
          document.querySelector('#apply').remove();
          setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<div data-test-job-application-status>Application submitted</div>'), 300);
        }''')
        self.assertIsNone(self.driver.wait_for_easy_apply('123', job, self.store, timeout=2))
        row = self.store.rows()[0]
        self.assertEqual(row['status'], 'applied')
        self.assertEqual(row['application_source'], 'linkedin_history')

    def test_disabled_easy_apply_does_not_click_or_claim_submission(self):
        job = self.driver.read_job(self.store.rows()[0]['url'])
        self.page.locator('#apply').evaluate('el => el.disabled = true')
        with self.assertRaisesRegex(NeedsInput, 'disabled or covered'):
            self.driver.wait_for_easy_apply('123', job, self.store, timeout=0.5)
        self.assertEqual(self.store.rows()[0]['status'], 'queued')
        self.assertFalse(self.page.locator('[role=dialog]').is_visible())

    def test_contract_badge_prevents_application_even_when_full_time(self):
        self.settings.permanent_full_time_only = True
        body = JOB.replace('>Remote</div>', '>Remote</div><div data-test-employment-type>Full-time / Contract</div>')
        self.page.route('**/*', lambda route: route.fulfill(body=body))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['status'], 'skipped')
        self.assertIn('permanent', self.store.rows()[0]['note'])
        self.assertFalse(self.page.locator('[role=dialog]').is_visible())
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_sdui_employment_metadata_excludes_recommendations(self):
        from easy_apply.page_data import SNAPSHOT
        self.page.set_content('''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
            <section aria-label="Primary content"><div><span>Full-time</span><span>Permanent</span><span>£80,000/yr</span></div>
            <div><h2>About the job</h2><p>Quantitative research.</p></div>
            <div><h2>Recommended jobs</h2><span>Contract</span><span>£600/day</span></div></section></div>''')
        snapshot = self.page.evaluate(SNAPSHOT)
        self.assertIn('Full-time', snapshot['employment_type'])
        self.assertNotIn('Contract', snapshot['employment_type'])
        self.assertNotIn('/day', snapshot['salary'])
        self.assertIsNone(Settings(permanent_full_time_only=True).rejection(snapshot))
        self.page.locator('span').nth(1).evaluate("el => el.textContent = 'Contract'")
        self.assertIsNotNone(Settings(permanent_full_time_only=True).rejection(self.page.evaluate(SNAPSHOT)))

    def test_structured_employment_type_and_salary_belong_to_current_job(self):
        from easy_apply.page_data import SNAPSHOT
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.page.set_content('''<script type="application/ld+json">[
            {"@type":"JobPosting","identifier":{"value":"456"},"employmentType":"CONTRACTOR"},
            {"@type":"JobPosting","identifier":{"value":"123"},"employmentType":"FULL_TIME","baseSalary":{"value":{"unitText":"DAY","value":600}}}
            ]</script>''')
        snapshot = self.page.evaluate(SNAPSHOT)
        self.assertNotIn('CONTRACTOR', snapshot['employment_type'])
        self.assertIn('FULL_TIME', snapshot['employment_type'])
        self.assertIsNotNone(Settings(permanent_full_time_only=True).rejection(snapshot))

    def test_sc_required_job_is_skipped_before_opening_application(self):
        self.settings.exclude_sc_required = True
        self.page.route('**/*', lambda route: route.fulfill(body=JOB.replace('Python and SQL engineering role.', 'Current SC clearance is required.')))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['status'], 'skipped')
        self.assertIn('SC clearance', self.store.rows()[0]['note'])
        self.assertFalse(self.page.locator('[role=dialog]').is_visible())
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_sdui_description_excludes_recommended_clearance_jobs(self):
        from easy_apply.page_data import SNAPSHOT
        self.page.set_content('''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
            <section aria-label="Primary content"><div><h2>About the job</h2>
            <p>Quantitative research with Python.</p></div>
            <div><h2>Recommended jobs</h2><p>SC clearance required.</p></div></section></div>''')
        description = self.page.evaluate(SNAPSHOT)['description']
        self.assertIn('Quantitative research', description)
        self.assertNotIn('SC clearance', description)
        self.page.locator('p').first.evaluate("el => el.textContent = 'SC clearance required.'")
        self.assertIn('SC clearance required', self.page.evaluate(SNAPSHOT)['description'])
        self.assertIsNotNone(Settings(exclude_sc_required=True).rejection(self.page.evaluate(SNAPSHOT)))

    def test_priority_refreshes_before_existing_queue_and_applies_new_quant_first(self):
        self.settings.newest_first = self.settings.prioritize_quant = True
        self.settings.max_applications = 1
        self.store.update('123', 'queued', job={'title': 'Data Scientist'})
        calls = []
        def discover(settings, store):
            calls.append(settings.keywords)
            store.add('https://www.linkedin.com/jobs/view/456/')
            store.save_listing_metadata('456', {'title': 'Quantitative Analyst', 'posted_datetime': '2026-10-01T00:00:00Z'})
        self.driver.discover = Mock(side_effect=discover)
        self.driver.run(self.settings, self.store)
        self.assertEqual(calls, [self.settings.keywords])
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(rows['123']['status'], 'queued')

    def test_blocked_fields_are_captured_and_saved_answers_fill_without_ai(self):
        self.page.set_content('''<dialog open>
            <label for="years">Years of software engineering experience*</label><input id="years" type="number" required>
            <fieldset><legend>Have you worked in a startup?*</legend>
            <label><input name="startup" type="radio" value="yes" required>Yes</label>
            <label><input name="startup" type="radio" value="no" required>No</label></fieldset>
            <label for="photo">Photo</label><input id="photo"></dialog>''')
        questions = []
        missing = fill_form(self.page.locator('dialog'), self.settings, unresolved=questions)
        self.assertEqual(len(questions), 2)
        self.assertEqual({q['kind'] for q in questions}, {'number', 'radio'})
        self.assertEqual(next(q['options'] for q in questions if q['kind'] == 'radio'), ['Yes', 'No'])
        self.store.record_questions('123', questions)
        self.store.update('123', 'needs_input', 'Save exact answers or resolve these fields: ' + ' | '.join(missing))
        self.assertEqual(len(self.store.unanswered_questions(self.settings)), 2)
        for spec in questions:
            self.settings.answers[spec['label']] = '3' if spec['kind'] == 'number' else 'Yes'
        generator = Mock()
        self.assertEqual(fill_form(self.page.locator('dialog'), self.settings, generator=generator), [])
        generator.answer.assert_not_called()
        self.assertEqual(self.page.locator('#years').input_value(), '3')
        self.assertTrue(self.page.locator('input[value=yes]').is_checked())

    def test_readonly_work_and_education_steps_are_ready_without_inputs(self):
        from easy_apply.navigation import STEP_STATE, wait_for_form_ready, wait_for_step_change
        self.page.set_content('<dialog open><h2>Apply to Example</h2><p>3/6 pages</p><input aria-label="Name"><button>Next</button></dialog>')
        previous = self.page.locator('dialog').evaluate(STEP_STATE)['key']
        for progress, title, content in [
            ('4/6 pages', 'Berufserfahrung', '<p>Your title</p><p>Data Scientist</p><p>Company</p><p>Example</p><p>Dates of employment</p><p>2025 - Present</p>'),
            ('5/6 pages', 'Education', '<p>School</p><p>Example University</p><p>Degree</p><p>MSc</p><p>Field of study</p><p>Analytics</p>')]:
            self.page.set_content(f'<dialog open><h2>Apply to Example</h2><p>{progress}</p><h3>{title}</h3>{content}<button>Add more</button><button>Next</button></dialog>')
            wait_for_step_change(self.page, previous, timeout=2)
            scope = wait_for_form_ready(self.page, timeout=2)
            self.assertEqual(scope.locator('input').count(), 0)
            previous = scope.evaluate(STEP_STATE)['key']
        self.page.set_content('<dialog open><h2>Apply to Example</h2><p>4/6 pages</p><h3>Work experience</h3><button>Next</button></dialog>')
        self.assertFalse(self.page.locator('dialog').evaluate(STEP_STATE)['ready'])

    def test_sdui_empty_label_radio_options_and_phd_need_no_llm(self):
        questions = [
            ('work', 'Are you legally authorized to work in United Kingdom?*', 'Yes'),
            ('sponsor', 'Will you now or in the future require sponsorship for employment visa status?*', 'Yes'),
            ('degree', 'Have you completed the following level of education: Doctor of Philosophy?*', 'No'),
        ]
        sections = []
        for name, question, answer in questions:
            options = ''.join(f'''<div><div>
                <input id="{name}-{value}" type="radio" name="{name}" aria-label="{question}" style="opacity:0;position:absolute;width:0;height:0" {'checked' if value == 'Yes' else ''}>
                <label for="{name}-{value}" style="display:inline-block;width:25px;height:25px;border:1px solid"></label>
                </div><div><p>{value}</p></div></div>''' for value in ['Yes', 'No'])
            sections.append(f'<div><p>{question}</p><fieldset role="radiogroup">{options}</fieldset></div>')
        self.page.set_content('<section role="dialog">' + ''.join(sections) + '</section>')
        self.settings.answers.update({question: answer for _, question, answer in questions[:2]})
        self.settings.answers['Do you have a PhD?'] = 'No'
        generator = Mock()
        self.assertEqual(fill_form(self.page.locator('[role=dialog]'), self.settings, generator=generator), [])
        for name, _, answer in questions:
            self.assertTrue(self.page.locator(f'#{name}-{answer}').is_checked())
        generator.answer.assert_not_called()

    def test_sdui_document_title_identifies_current_job_not_recommendation(self):
        from easy_apply.page_data import SNAPSHOT
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.page.set_content('''<title>(2) Quantitative Engineer | Example Ltd | LinkedIn</title>
            <section aria-label="Primary content"><p>Quantitative Engineer</p>
            <aside><p>Another job</p></aside></section>''')
        job = self.page.evaluate(SNAPSHOT)
        self.assertEqual((job['title'], job['company']), ('Quantitative Engineer', 'Example Ltd'))
        self.page.evaluate("document.title = 'Jobs | LinkedIn'")
        self.assertEqual(self.page.evaluate(SNAPSHOT)['title'], '')

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "jobs.sqlite3")
        self.store.add("https://www.linkedin.com/jobs/view/123/")
        self.settings = Settings(first_name="Ada", last_name="Example", email="ada@example.com", keywords="Python", location="London", answers={"Do you need sponsorship?": "Yes"})
        self.driver = LinkedInBrowser(threading.Event(), lambda text: None)
        self.driver.page = self.page
        # Most form tests isolate the queue; discovery has separate coverage.
        self.driver.discover = Mock(return_value=0)
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=JOB))

    def test_uk_phone_code_and_formats_need_no_ai(self):
        generator = Mock()
        self.settings.phone = '+44 7700900123'
        for attributes, expected in [('', '7700900123'), ('pattern="0[0-9]{10}"', '07700900123'),
                                     ('minlength="11" maxlength="11"', '07700900123'),
                                     ('maxlength="10"', '7700900123'),
                                     ('placeholder="07123 456789"', '07700900123')]:
            with self.subTest(attributes=attributes):
                # Number first deliberately: country selection must happen first.
                self.page.set_content(f'''<section role="dialog">
                    <label for="phone">Mobile phone number*</label><input id="phone" type="tel" required {attributes}>
                    <label for="code">Phone country code*</label><select id="code" required>
                    <option value="">Choose</option><option value="US">United States (+1)</option>
                    <option value="JE">Jersey (+44)</option><option value="GB">United Kingdom (+44)</option></select>
                    </section>''')
                missing = fill_form(self.page.locator('[role=dialog]'), self.settings, generator=generator)
                self.assertEqual(missing, [])
                self.assertEqual(self.page.locator('#code').input_value(), 'GB')
                self.assertEqual(self.page.locator('#phone').input_value(), expected)
        generator.answer.assert_not_called()

    def test_uk_phone_without_code_and_domestic_profile(self):
        for profile, country, attributes, expected in [
            ('+447700900123', '', '', '+447700900123'),
            ('07700900123', 'United Kingdom', 'maxlength="11"', '07700900123'),
            ('7700900123', 'UK', 'pattern="0[0-9]{10}"', '07700900123'),
        ]:
            with self.subTest(profile=profile):
                self.settings.phone, self.settings.country = profile, country
                self.page.set_content(f'<section role="dialog"><label for="phone">Phone number*</label><input id="phone" type="tel" {attributes}></section>')
                self.assertEqual(fill_form(self.page.locator('[role=dialog]'), self.settings), [])
                self.assertEqual(self.page.locator('#phone').input_value(), expected)

    def test_uk_phone_does_not_truncate_or_guess_ambiguous_code(self):
        self.settings.phone = '+447700900123'
        generator = Mock()
        self.page.set_content('''<section role="dialog"><label for="code">Phone country code*</label>
            <select id="code"><option value="A">+44</option><option value="B">+44</option></select>
            <label for="phone">Phone number*</label><input id="phone" type="tel" maxlength="8"></section>''')
        missing = fill_form(self.page.locator('[role=dialog]'), self.settings, generator=generator)
        self.assertEqual(len(missing), 2)
        self.assertEqual(self.page.locator('#phone').input_value(), '')
        generator.answer.assert_not_called()

    def test_uk_code_does_not_change_residence_country(self):
        self.settings.phone, self.settings.country = '+447700900123', 'France'
        self.page.set_content('''<section role="dialog"><label for="country">Country*</label>
            <select id="country"><option>United Kingdom</option><option>France</option></select>
            <label for="code">Phone country code*</label><select id="code"><option value="44">+44</option></select>
            <label for="phone">Phone*</label><input id="phone" type="tel"></section>''')
        self.assertEqual(fill_form(self.page.locator('[role=dialog]'), self.settings), [])
        self.assertEqual(self.page.locator('#country').input_value(), 'France')
        self.assertEqual(self.page.locator('#phone').input_value(), '7700900123')

    def test_phone_retries_national_when_client_validation_rejects_subscriber(self):
        self.settings.phone = '+447700900123'
        self.page.set_content('''<section role="dialog"><label for="code">Phone country code*</label>
            <select id="code"><option value="GB">United Kingdom (+44)</option></select>
            <label for="phone">Phone*</label><input id="phone" type="tel"
            oninput="this.setCustomValidity(this.value.startsWith('0') ? '' : 'Use a leading zero')"></section>''')
        self.assertEqual(fill_form(self.page.locator('[role=dialog]'), self.settings), [])
        self.assertEqual(self.page.locator('#phone').input_value(), '07700900123')

    def test_phone_waits_for_delayed_blur_validation_before_accepting_format(self):
        self.settings.phone = '+447700900123'
        self.page.set_content('''<section role="dialog"><label for="code">Phone country code*</label>
            <select id="code"><option value="GB">United Kingdom (+44)</option></select>
            <label for="phone">Phone*</label><input id="phone" type="tel"
            onblur="setTimeout(()=>this.setAttribute('aria-invalid',String(!this.value.startsWith('0'))),100)"></section>''')
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings), [])
        self.assertEqual(self.page.locator('#phone').input_value(), '07700900123')

    def test_complete_flow_submits_once_and_records_confirmation(self):
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.store.rows()[0]["status"], "applied")
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_optional_questions_do_not_fill_call_ai_or_block_submission(self):
        self.settings.city = 'London'
        self.settings.answers.update({'Photo': 'saved photo', 'Optional choice': 'Yes'})
        optional = '''<label for="photo">Photo</label><input id="photo">
            <label for="city">Location (city)</label><input id="city">
            <label for="other">Other details</label><textarea id="other"></textarea>
            <label for="choice">Optional choice</label><select id="choice"><option value="">Select</option><option>Yes</option></select>
            <fieldset><legend>Optional choice</legend><label><input id="optional-radio" name="optional" type="radio">Yes</label></fieldset>
            <label for="file">Photo upload</label><input id="file" type="file">
            <div contenteditable="true" aria-label="Cover letter"></div>
            <label for="prefilled">Optional note</label><input id="prefilled" value="Existing value">'''
        content = JOB.replace('<button type="submit">Next', optional + '<button type="submit">Next')
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.page.goto('https://www.linkedin.com/jobs/view/123/')
        self.page.locator('#apply').click()
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        for selector in ['#photo', '#city', '#other', '#choice', '#file']:
            self.assertEqual(self.page.locator(selector).input_value(), '')
        self.assertFalse(self.page.locator('#optional-radio').is_checked())
        self.assertEqual(self.page.locator('#prefilled').input_value(), 'Existing value')
        generator.answer.assert_not_called()
        # Run the entire mocked application to verify optional fields do not
        # prevent navigation or confirmation. All network routes are fixtures.
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_required_signals_and_radio_group_member(self):
        self.page.set_content('''<section role="dialog">
            <label for="first">First name*</label><input id="first" aria-label="First name">
            <label for="last">Last name</label><input id="last" required>
            <label for="email">Email address</label><input id="email" aria-required="true">
            <fieldset><legend>Do you need sponsorship?</legend>
            <label><input id="no" type="radio" name="sponsor" required>No</label>
            <label><input id="yes" type="radio" name="sponsor">Yes</label></fieldset>
            <div contenteditable="true" aria-required="true" aria-label="Statement"></div>
            </section>''')
        missing = fill_form(self.page.get_by_role('dialog'), self.settings)
        self.assertEqual(missing, ['Unsupported required rich-text question; complete manually'])
        self.assertEqual(self.page.locator('#first').input_value(), 'Ada')
        self.assertEqual(self.page.locator('#last').input_value(), 'Example')
        self.assertEqual(self.page.locator('#email').input_value(), 'ada@example.com')
        self.assertTrue(self.page.locator('#yes').is_checked())

    def test_optional_resume_and_phone_code_are_left_unchanged(self):
        self.settings.phone = '+447700900123'
        self.page.set_content('''<section role="dialog"><h2>Resume</h2>
            <div data-test-resume-card><label><input id="first" name="cv" type="radio">first.pdf</label></div>
            <div data-test-resume-card><label><input id="second" name="cv" type="radio" checked>second.pdf</label></div>
            <label for="upload">Resume</label><input id="upload" type="file">
            <label for="code">Phone country code</label><select id="code"><option>United Kingdom (+44)</option></select>
            <label for="phone">Phone*</label><input id="phone" type="tel">
            </section>''')
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertFalse(self.page.locator('#first').is_checked())
        self.assertTrue(self.page.locator('#second').is_checked())
        self.assertEqual(self.page.locator('#upload').input_value(), '')
        self.assertEqual(self.page.locator('#phone').input_value(), '7700900123')
        generator.answer.assert_not_called()

    def test_unknown_required_answer_is_recorded_without_submission(self):
        self.settings.answers = {}
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "needs_input")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_question_blocker_continues_to_next_job(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.context.unroute('**/*')
        blocked = JOB.replace('Email address *', 'Unknown mandatory question *')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body=blocked if '/123/' in route.request.url else JOB))
        self.driver.run(self.settings, self.store)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'needs_input')
        self.assertIn('Unknown mandatory question', rows['123']['note'])
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_relocation_question_uses_preference_without_ai(self):
        self.settings.willing_relocate_anywhere = True
        self.page.set_content('''<section role="dialog"><fieldset><legend>Are you willing to relocate to Edinburgh?*</legend>
            <label><input id="yes" type="radio" name="move">Yes</label>
            <label><input type="radio" name="move">No</label></fieldset></section>''')
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertTrue(self.page.locator('#yes').is_checked())
        generator.answer.assert_not_called()

    def test_submission_cap_leaves_other_jobs_queued(self):
        self.store.add("https://www.linkedin.com/jobs/view/456/")
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        self.assertEqual(len(self.store.rows("applied")), 1)
        self.assertEqual(len(self.store.rows("queued")), 1)

    def test_queue_prioritizes_titles_matching_current_search(self):
        self.store.update('123', 'queued', job={'title':'Other occupation'})
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.store.update('456', 'queued', job={'title':'Python Engineer'})
        self.settings.title_include = 'engineer'
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        rows = {r['id']:r for r in self.store.rows()}
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(rows['123']['status'], 'queued')

    def test_saved_listing_title_fills_missing_page_metadata_for_filters(self):
        self.store.update('123', 'queued', job={'title':'Quantitative Researcher'})
        self.settings.title_include = 'quantitative'
        self.context.unroute('**/*')
        content = JOB.replace('<h1>Python Engineer</h1>', '')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_exhausted_filtered_queue_refreshes_without_opening_unrelated_jobs(self):
        self.store.update('123', 'queued', job={'title': 'Data Scientist'})
        self.settings.title_include = 'engineer'
        self.settings.max_applications = 1
        def discover(settings, store):
            store.add('https://www.linkedin.com/jobs/view/456/')
            store.save_listing_metadata('456', {'title': 'Python Engineer'})
        self.driver.discover = Mock(side_effect=discover)
        visited = []
        self.context.unroute('**/*')
        def respond(route):
            visited.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=JOB)
        self.context.route('**/*', respond)
        self.driver.run(self.settings, self.store)
        self.driver.discover.assert_called_once()
        rows = {r['id']:r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'queued')
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(visited, [rows['456']['url']])

    def test_filtered_queue_with_no_results_finishes_without_repeated_scans(self):
        logs = []
        self.driver.log = logs.append
        self.store.update('123', 'queued', job={'title': 'Data Scientist'})
        self.settings.title_include = 'quantitative'
        self.driver.run(self.settings, self.store)
        self.driver.discover.assert_called_once()
        self.assertEqual(self.store.rows()[0]['status'], 'queued')
        self.assertEqual(self.page.url, 'about:blank')
        self.assertIn('No queued jobs match the current title filters after searching.', logs[-1])

    def test_unknown_queued_title_gets_live_check_after_refresh(self):
        self.settings.title_include = 'engineer'
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        self.driver.discover.assert_called_once()
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_queue_refills_from_search_and_applies_next_job(self):
        self.context.unroute("**/*")
        visited = []
        def respond(route):
            visited.append(route.request.url)
            content = '<main><a href="/jobs/view/456/">Next job</a><a href="/jobs/view/789/">Another job</a></main>' if '/jobs/search/' in route.request.url else JOB
            route.fulfill(status=200, content_type="text/html", body=content)
        self.context.route("**/*", respond)
        self.driver.discover = LinkedInBrowser.discover.__get__(self.driver)
        self.settings.max_applications = 2
        self.driver.run(self.settings, self.store)
        self.assertEqual({r['id'] for r in self.store.rows('applied')}, {'123', '456'})
        self.assertEqual([r['id'] for r in self.store.rows('queued')], ['789'])
        self.assertEqual(sum('/jobs/search/' in url for url in visited), 1)

    def test_start_with_empty_queue_searches_once_and_finishes_if_no_new_jobs(self):
        self.store.update('123', 'applied')
        self.driver.run(self.settings, self.store)
        self.driver.discover.assert_called_once_with(self.settings, self.store)

    def test_rate_warning_before_submit_pauses_then_continues(self):
        self.context.unroute("**/*")
        visits = []
        def respond(route):
            visits.append(route.request.url)
            content = '<main><div role="alert">You are applying too fast. Please try again later.</div></main>' if len(visits) == 1 else JOB
            route.fulfill(status=200, content_type="text/html", body=content)
        self.context.route("**/*", respond)
        self.driver.pause_for_limit = Mock()
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        self.driver.pause_for_limit.assert_called_once()
        self.assertEqual(self.driver.pause_for_limit.call_args.args[0].seconds, 600)
        self.assertEqual(self.store.rows()[0]['status'], 'applied')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_persistent_rate_warning_stops_after_two_pauses(self):
        self.context.unroute("**/*")
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main><div role="alert">You are applying too fast</div></main>'))
        self.driver.pause_for_limit = Mock()
        with self.assertRaisesRegex(NeedsInput, 'after two pauses'):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.driver.pause_for_limit.call_count, 2)
        self.assertEqual(self.store.rows()[0]['status'], 'queued')

    def test_temporarily_paused_modal_waits_then_resumes_without_missing_button_error(self):
        self.context.unroute('**/*')
        visits = []
        def respond(route):
            visits.append(route.request.url)
            warning = '''<main><h1>Machine Learning Engineer</h1></main><div role="dialog">
                <h2>Easy Apply is temporarily paused</h2><p>We noticed you’re applying at a fast pace.
                You can continue shortly.</p><button>Got it</button></div>'''
            route.fulfill(content_type='text/html; charset=utf-8', body=warning if len(visits) == 1 else JOB)
        self.context.route('**/*', respond)
        self.driver.pause_for_limit = Mock()
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        self.driver.pause_for_limit.assert_called_once()
        self.assertEqual(self.driver.pause_for_limit.call_args.args[0].seconds, 600)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_temporarily_paused_text_in_hidden_or_description_is_not_limit(self):
        self.page.set_content('''<main><h1>ML Engineer</h1>
            <div hidden role="dialog"><h2>Easy Apply is temporarily paused</h2></div>
            <div id="job-details"><h2>Easy Apply is temporarily paused</h2></div>
            <aside><p>We noticed you’re applying at a fast pace</p></aside></main>''')
        self.assertIsNone(visible_limit(self.page))

    def test_daily_maximum_stops_before_submit_and_preserves_queue(self):
        self.context.unroute("**/*")
        content = JOB.replace('</body>', '<div aria-modal="true"><h2>You reached today’s Easy Apply limit</h2><p>Save this job and continue applying tomorrow.</p><button>Got it</button></div></body>')
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=content))
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.driver.pause_for_limit = Mock()
        from easy_apply.browser import DailyLimitReached
        with self.assertRaisesRegex(DailyLimitReached, 'daily application maximum') as caught:
            self.driver.run(self.settings, self.store)
        self.assertEqual(caught.exception.confirmed_in_run, 0)
        self.assertIn('today’s Easy Apply limit', caught.exception.linkedin_message)
        self.assertEqual(len(self.store.rows('queued')), 2)
        self.assertIsNone(self.page.evaluate('window.sends'))
        self.driver.pause_for_limit.assert_not_called()
        self.driver.discover.assert_not_called()

    def test_daily_warning_after_submit_never_retries_or_opens_next_job(self):
        self.context.unroute("**/*")
        content = JOB.replace('<h2>Application sent</h2>', '<h2>You reached today’s Easy Apply limit</h2>')
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=content))
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        with self.assertRaisesRegex(NeedsInput, 'daily application maximum'):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual([r['id'] for r in self.store.rows('unverified')], ['123'])
        self.assertEqual([r['id'] for r in self.store.rows('queued')], ['456'])

    def test_rate_warning_after_submit_verifies_before_continuing(self):
        self.context.unroute("**/*")
        visits = []
        sends = []
        self.context.expose_binding('recordSend', lambda source: sends.append(source['page'].url))
        def respond(route):
            visits.append(route.request.url)
            content = JOB.replace('window.sends=', 'window.recordSend();window.sends=', 1)
            if '/123/' in route.request.url:
                if visits.count(route.request.url) == 1:
                    content = content.replace('<h2>Application sent</h2>', '<h2 role=alert>You are applying too fast</h2>')
                else:
                    content = '<main><h1>Python Engineer</h1><div class="jobs-s-apply">Applied today</div></main>'
            route.fulfill(status=200, content_type="text/html", body=content)
        self.context.route("**/*", respond)
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.settings.max_applications = 2
        self.driver.pause_for_limit = Mock()
        self.driver.run(self.settings, self.store)
        self.driver.pause_for_limit.assert_called_once()
        self.assertEqual(len(self.store.rows('applied')), 2)
        self.assertEqual(sends, ['https://www.linkedin.com/jobs/view/123/', 'https://www.linkedin.com/jobs/view/456/'])

    def test_rate_warning_with_uncertain_submission_never_resubmits(self):
        self.context.unroute("**/*")
        content = JOB.replace('<h2>Application sent</h2>', '<h2 role=alert>You are applying too fast</h2>')
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=content))
        self.driver.pause_for_limit = Mock()
        with self.assertRaisesRegex(NeedsInput, 'last submission is unverified'):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['status'], 'unverified')
        self.assertIsNone(self.page.evaluate('window.sends'))  # Fresh page was not submitted again.
        self.assertFalse(self.page.get_by_role('dialog').is_visible())

    def test_limit_detector_ignores_hidden_messages_and_job_description(self):
        self.page.set_content('''<main><div role="alert" hidden>You are applying too fast</div>
          <div id="job-details"><p>You have reached the daily application limit</p></div>
          <aside><p>You are applying too fast</p></aside><h1>Application Engineer</h1></main>''')
        self.assertIsNone(visible_limit(self.page))

    def test_browser_failure_during_cooldown_verification_preserves_unverified(self):
        self.context.unroute("**/*")
        content = JOB.replace('<h2>Application sent</h2>', '<h2 role=alert>You are applying too fast</h2>')
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=content))
        self.driver.pause_for_limit = Mock()
        original = self.driver.read_job
        calls = []
        def read_job(url):
            calls.append(url)
            if len(calls) > 1:
                raise RuntimeError('Browser disconnected during verification')
            return original(url)
        self.driver.read_job = read_job
        with self.assertRaisesRegex(NeedsInput, 'Browser interaction failed'):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['status'], 'unverified')
        self.assertFalse(self.store.requeue('123'))

    def test_already_applied_continues_to_next_job_without_using_cap(self):
        self.context.unroute("**/*")
        previous = '''<main><h1>Previous job</h1><div class="jobs-s-apply">
        <span class="jobs-s-apply__applied-date">Applied on October 12, 2026</span>
        </div></main>'''
        visited = []
        def respond(route):
            visited.append(route.request.url)
            route.fulfill(status=200, content_type="text/html",
                          body=previous if "/123/" in route.request.url else JOB)
        self.context.route("**/*", respond)
        self.store.add("https://www.linkedin.com/jobs/view/456/")
        self.store.add("https://www.linkedin.com/jobs/view/789/")
        self.settings.max_applications = 1
        self.settings.title_exclude = "Previous"
        logs = []
        self.driver.log = logs.append
        self.driver.run(self.settings, self.store)
        rows = {row["id"]: row for row in self.store.rows()}
        self.assertEqual(rows["123"]["status"], "applied")
        self.assertEqual(rows['123']['application_source'], 'linkedin_history')
        self.assertIn("No new submission", rows["123"]["note"])
        self.assertEqual(rows["456"]["status"], "applied")
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(rows["789"]["status"], "queued")
        self.assertEqual(visited, [rows["123"]["url"], rows["456"]["url"]])
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertIn("Run finished: 1 confirmed applications. Run limit reached (1).", logs)
        self.assertFalse(self.store.requeue("123"))

    def test_applied_status_variants_and_hidden_or_unrelated_text(self):
        for label in ["Applied", "You already applied", "You've already applied",
                      "You applied on 12 October 2026", "Applied 2 days ago",
                      "Application submitted", "Your application was sent to Example"]:
            with self.subTest(label=label):
                self.page.set_content(f'<main><div class="jobs-s-apply"><span>{label}</span></div></main>')
                self.assertEqual(self.page.evaluate(APPLIED_STATUS), label)
        self.page.set_content('''<main>
          <div class="jobs-s-apply" hidden>Applied</div>
          <div class="jobs-s-apply" style="visibility:hidden">Applied yesterday</div>
          <div class="job-details-jobs-unified-top-card"><h1>Applied Scientist</h1></div>
          <div id="job-details"><div class="jobs-s-apply">You already applied</div></div>
          <aside><div class="jobs-s-apply">Applied</div></aside>
          <div class="job-card-container"><div class="jobs-s-apply">Applied</div></div>
          <div class="jobs-s-apply">Apply now</div>
        </main>''')
        self.assertEqual(self.page.evaluate(APPLIED_STATUS), "")

    def test_sdui_application_status_card_continues_queue_without_new_send(self):
        self.context.unroute('**/*')
        # Reduced from Referment 4478507773: status is below the header, with
        # generated classes, nested wrappers and no Easy Apply button.
        previous = '''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
            <section aria-label="Primary content"><p>Credit Quantitative Analyst</p>
            <div style="height:1200px">Take the next step in your job search</div>
            <div><div><h2>Application status</h2></div><div><div>
            <p>Application submitted</p><p>1 day ago</p><button>View resume</button>
            </div></div></div><div><h2>About the job</h2><p>Python</p></div></section></div>'''
        visited = []
        def respond(route):
            visited.append(route.request.url)
            route.fulfill(status=200, content_type='text/html', body=previous if '/123/' in route.request.url else JOB)
        self.context.route('**/*', respond)
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.store.add('https://www.linkedin.com/jobs/view/789/')
        self.settings.max_applications = 1
        self.driver.run(self.settings, self.store)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows['123']['application_source'], 'linkedin_history')
        self.assertIn('Application submitted. No new submission.', rows['123']['note'])
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(rows['789']['status'], 'queued')
        self.assertEqual(visited, [rows['123']['url'], rows['456']['url']])
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_sdui_status_card_ignores_unrelated_hidden_and_incomplete_cards(self):
        card = '<div><div><h2>Application status</h2></div><div><p>Application submitted</p><p>1 day ago</p></div></div>'
        primary = '<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"><section aria-label="Primary content">{}</section></div>'
        for content in [
            '<div hidden>' + card + '</div>',
            '<aside>' + card + '</aside>',
            '<div componentkey="job-card-component-ref-456">' + card + '</div>',
            '<div id="job-details">' + card + '</div>',
            '<h2>About the job</h2>' + card,
            '<h2>More jobs</h2>' + card,
            '<div><h2>Application status</h2></div><div><h2>Other job</h2><p>Application submitted</p></div>',
            '<div><h2>Application status</h2><p>Application started</p></div>',
        ]:
            with self.subTest(content=content):
                self.page.set_content(primary.format(content))
                self.assertEqual(self.page.evaluate(APPLIED_STATUS), '')
        self.page.set_content(card)  # A similarly named card outside a job page.
        self.assertEqual(self.page.evaluate(APPLIED_STATUS), '')

    def test_delayed_sdui_status_card_is_detected_without_apply_button(self):
        self.context.unroute('**/*')
        content = '''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
            <section aria-label="Primary content"><div><h2>Application status</h2><p id="status"></p></div></section></div>
            <script>setTimeout(()=>document.querySelector('#status').textContent='Application submitted',250)</script>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['application_source'], 'linkedin_history')
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_existing_application_never_opens_form_even_with_apply_button(self):
        self.context.unroute("**/*")
        content = JOB.replace('<h1>', '<div class="jobs-s-apply">Applied yesterday</div><h1>', 1)
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "applied")
        self.assertFalse(self.page.get_by_role("dialog").is_visible())
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_delayed_applied_status_is_detected_while_waiting_for_page(self):
        self.context.unroute("**/*")
        content = '''<main><h1>Previous job</h1><div class="jobs-s-apply"></div></main>
        <script>setTimeout(() => document.querySelector('.jobs-s-apply').textContent = 'Applied today', 200)</script>'''
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "applied")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_closed_sdui_listing_skips_and_continues_without_using_cap(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.settings.max_applications = 1
        self.context.unroute('**/*')
        # Reduced from the PeopleGenius page, whose SDUI header has no h1.
        closed = '''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"><main>
            <section aria-label="Primary content"><p>Data Scientist</p>
            <div aria-live="assertive" aria-atomic="true"><p>No longer accepting applications</p></div>
            </section></main></div>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body=closed if '/123/' in route.request.url else JOB))
        self.driver.run(self.settings, self.store)
        rows = {r['id']:r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'skipped')
        self.assertEqual(rows['123']['note'], 'Closed listing: No longer accepting applications')
        self.assertEqual(rows['123']['application_source'], '')
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_delayed_closed_notice_is_detected_during_page_load(self):
        self.context.unroute('**/*')
        content = '''<main><h1>Closed job</h1><div class="jobs-s-apply"></div></main>
            <script>setTimeout(()=>document.querySelector('.jobs-s-apply').textContent='No longer accepting applications',200)</script>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]['status'], 'skipped')
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_finalto_polite_closed_notice_continues_to_next_job(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.settings.max_applications = 1
        closed = '''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"><main>
            <section aria-label="Primary content"><div><h1>Trading Solutions Analyst</h1>
            <p>Finalto</p><div aria-live="polite" aria-atomic="true"><p>Not currently accepting applications</p></div></div>
            <div><h2>About the job</h2><p>Trading systems.</p></div></section></main></div>'''
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body=closed if '/123/' in route.request.url else JOB))
        self.driver.run(self.settings, self.store)
        rows = {row['id']:row for row in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'skipped')
        self.assertEqual(rows['123']['note'], 'Closed listing: Not currently accepting applications')
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)

    def test_polite_closure_ignores_sdui_description_recommendations_and_hidden_notice(self):
        self.page.set_content('''<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails">
          <section aria-label="Primary content"><div aria-live="polite" hidden>Not currently accepting applications</div>
          <aside><div aria-live="polite">Not currently accepting applications</div></aside>
          <h2>About the job</h2><div aria-live="polite">Not currently accepting applications</div>
          <h2>Recommended jobs</h2><div aria-live="polite">Not currently accepting applications</div></section></div>''')
        self.assertEqual(self.page.evaluate(CLOSED_STATUS), '')

    def test_polite_closure_appearing_while_waiting_for_easy_apply_is_skipped(self):
        job = self.driver.read_job(self.store.rows()[0]['url'])
        self.page.evaluate('''() => {
          document.querySelector('#apply').remove();
          setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"><section aria-label="Primary content"><div aria-live="polite">Not currently accepting applications</div></section></div>'), 200);
        }''')
        self.assertIsNone(self.driver.wait_for_easy_apply('123', job, self.store, timeout=2))
        self.assertEqual(self.store.rows()[0]['status'], 'skipped')
        self.assertIsNone(self.page.evaluate('window.sends'))

    def test_closed_notice_ignores_descriptions_recommendations_and_hidden_text(self):
        self.page.set_content('''<main><div class="jobs-s-apply" hidden>No longer accepting applications</div>
            <div id="job-details"><div class="jobs-s-apply">No longer accepting applications</div></div>
            <aside><div class="jobs-s-apply">No longer accepting applications</div></aside>
            <div class="job-card-container"><div class="jobs-s-apply">No longer accepting applications</div></div>
            <div class="jobs-s-apply">This other role is no longer accepting applications</div></main>''')
        self.assertEqual(self.page.evaluate(CLOSED_STATUS), '')
        self.page.set_content('<main><div class="jobs-s-apply">No longer accepting applications</div><div class="jobs-s-apply__applied-date">Applied today</div></main>')
        # A previously applied closed job must keep application history.
        job = {'applied_status': 'Applied today', 'closed_status': 'No longer accepting applications'}
        self.driver.read_job = Mock(return_value=job)
        self.assertFalse(self.driver.apply(self.store.rows()[0], self.settings, self.store))
        self.assertEqual(self.store.rows()[0]['application_source'], 'linkedin_history')

    def test_filters_block_before_opening_application(self):
        self.settings.title_exclude = "Engineer"
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "skipped")
        self.assertFalse(self.page.get_by_role("dialog").is_visible())

    def test_stop_prevents_navigation_and_submission(self):
        self.driver.stop_event.set()
        with self.assertRaises(Stopped):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "queued")

    def test_missing_confirmation_never_retries(self):
        self.context.unroute("**/*")
        broken = JOB.replace("<h2>Application sent</h2>", "<h2>Pending</h2>")
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=broken))
        with self.assertRaises(NeedsInput):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "unverified")
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertFalse(self.store.requeue("123"))

    def test_select_consent_and_resume_use_explicit_answers(self):
        self.page.set_content('''<section role="dialog"><label for="country">Country *</label><select id="country" required><option value="">Select</option><option value="GB">United Kingdom</option></select>
        <label><input id="consent" type="checkbox" required>Accept conditions *</label>
        <label><input id="follow" type="checkbox" checked>Follow company</label>
        <label for="resume">Resume *</label><input id="resume" type="file" required></section>''')
        resume = Path(self.temp.name) / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4\n%%EOF")
        self.settings.resume = str(resume)
        self.settings.resume_source = "local_file"
        self.settings.answers = {"Country": "United Kingdom", "Accept conditions": "Yes"}
        missing = fill_form(self.page.get_by_role("dialog"), self.settings)
        self.assertEqual(missing, [])
        self.assertEqual(self.page.locator("#country").input_value(), "GB")
        self.assertTrue(self.page.locator("#consent").is_checked())
        self.assertFalse(self.page.locator("#follow").is_checked())
        self.assertEqual(self.page.locator("#resume").evaluate("e => e.files[0].name"), "resume.pdf")

    def test_review_hidden_follow_checkbox_uses_visible_empty_label(self):
        # Actual Primus review shape: hidden checked input, empty styled label,
        # and visible Follow text outside the label. No ARIA checkbox wrapper.
        self.context.unroute('**/*')
        content = JOB.replace('<p>Review your application</p>', '''<p>Review your application</p>
            <div><div><input id="follow" type="checkbox" checked style="display:none">
            <label for="follow" style="display:block;width:20px;height:20px;background:green"></label></div>
            <p>Follow Example to stay up to date with their page</p></div>''')
        content = content.replace("window.sends=(window.sends||0)+1;", "window.followAtSubmit=document.querySelector('#follow').checked;window.sends=(window.sends||0)+1;")
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertFalse(self.page.evaluate('window.followAtSubmit'))
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_hidden_required_checkbox_uses_explicit_answer_and_visible_wrapper(self):
        self.page.set_content('''<section role="dialog"><div role="checkbox" aria-label="Accept conditions"
            onclick="this.querySelector('input').checked=!this.querySelector('input').checked">
            <input id="consent" type="checkbox" required aria-label="Accept conditions" style="display:none">Accept conditions*</div></section>''')
        dialog = self.page.get_by_role('dialog')
        self.assertIn('Accept conditions', fill_form(dialog, self.settings))
        self.assertFalse(self.page.locator('#consent').is_checked())
        self.settings.answers['Accept conditions'] = 'Yes'
        self.assertEqual(fill_form(dialog, self.settings), [])
        self.assertTrue(self.page.locator('#consent').is_checked())
        self.assertEqual(fill_form(dialog, self.settings), [])
        self.assertTrue(self.page.locator('#consent').is_checked())

    def test_checkbox_that_does_not_toggle_blocks_only_that_job(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        self.context.unroute('**/*')
        broken = JOB.replace('<p>Review your application</p>', '''<p>Review your application</p>
            <input id="follow" type="checkbox" checked style="display:none">
            <label for="follow" onclick="event.preventDefault()">Follow company</label>''')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body=broken if '/123/' in route.request.url else JOB))
        self.driver.run(self.settings, self.store)
        rows = {r['id']:r for r in self.store.rows()}
        self.assertEqual(rows['123']['status'], 'needs_input')
        self.assertIn('could not verify it was unticked', rows['123']['note'])
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')

    def test_first_uploaded_resume_selected_without_local_file_or_ai(self):
        self.page.set_content('''<section role="dialog"><h2>Resume*</h2>
        <div data-test-resume-card><label><input type="radio" name="resume" id="first">first.pdf</label></div>
        <div data-test-resume-card><label><input type="radio" name="resume" id="second" checked>second.pdf</label></div>
        <label for="resume-upload">Resume</label><input id="resume-upload" type="file"></section>''')
        generator = Mock()
        missing = fill_form(self.page.get_by_role("dialog"), self.settings, generator=generator)
        self.assertEqual(missing, [])
        self.assertTrue(self.page.locator("#first").is_checked())
        self.assertFalse(self.page.locator("#second").is_checked())
        self.assertEqual(self.page.locator("#resume-upload").evaluate("e=>e.files.length"), 0)
        generator.answer.assert_not_called()

    def test_cv_filename_siblings_and_unnamed_options_do_not_become_questions(self):
        for markup in [
            '<div><input id="first" type="radio"><span>first.pdf</span></div><div><input id="second" type="radio"><span>second.docx</span></div>',
            '<input id="first" type="radio" aria-labelledby="file1"><span id="file1">first.pdf</span><input id="second" type="radio" aria-labelledby="file2"><span id="file2">second.pdf</span>',
            '<label><input id="first" type="radio">first.pdf</label><label><input id="second" type="radio">second.pdf</label>',
            '<div hidden><input type="radio"><span>hidden.pdf</span></div><label><input id="first" type="radio" style="display:none">first.pdf</label>',
        ]:
            with self.subTest(markup=markup):
                self.page.set_content('<section role="dialog"><h2>CV*</h2>' + markup + '</section>')
                generator = Mock()
                self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
                self.assertTrue(self.page.locator('#first').is_checked())
                generator.answer.assert_not_called()

    def test_live_resume_layout_nested_radios_and_paragraph_heading(self):
        # Reduced from the actual Quik Hire form: Resume is a paragraph, each
        # native input has an empty label and is nested inside a role=radio.
        cards = ''.join(f'''<div><div><div role="button"><p>PDF</p><p>candidate.pdf</p><p>{date}</p></div>
            <div role="radio" aria-label="candidate.pdf"><div aria-label="candidate.pdf">
            <input id="cv{i}" type="radio" name="radio-group-generated" {'checked' if i == 1 else ''}>
            <label for="cv{i}"></label></div></div></div></div>'''
            for i, date in enumerate(['10/12/2026', '8/31/2026', '8/9/2026']))
        self.page.set_content(f'''<section role="dialog"><header><h2>Apply to Example</h2></header>
            <div><div><p>Resume*</p><p>Select or upload a resume in DOC, DOCX, or PDF format</p></div>
            <div><fieldset role="radiogroup"><div>{cards}</div></fieldset></div></div></section>''')
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertTrue(self.page.locator('#cv0').is_checked())
        self.assertEqual(self.page.locator('input:checked').count(), 1)
        self.assertEqual(self.page.locator('input[data-ea-resume-choice]').count(), 3)
        generator.answer.assert_not_called()

    def test_paragraph_radio_questions_use_clearance_and_saved_location_facts(self):
        questions = [('Do you hold active SC Clearance or higher?*', 'clearance'),
                     ('Are you willing to work on-site in Manchester when required?*', 'onsite')]
        sections = ''.join(f'''<div><p>{question}</p><fieldset role="radiogroup">
            <div role="radio" aria-label="{question}" onclick="this.querySelector('input').checked=true"><div><input style="display:none" type="radio" name="{name}" id="{name}-yes"><label for="{name}-yes"></label><span>Yes</span></div></div>
            <div role="radio" aria-label="{question}" onclick="this.querySelector('input').checked=true"><div><input style="display:none" type="radio" name="{name}" id="{name}-no"><label for="{name}-no"></label><span>No</span></div></div>
            </fieldset></div>''' for question, name in questions)
        self.page.set_content('<section role="dialog"><h2>Additional Questions</h2>' + sections + '</section>')
        self.settings.no_security_clearance = True
        self.settings.answers[questions[1][0]] = 'Yes'
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertTrue(self.page.locator('#clearance-no').is_checked())
        self.assertTrue(self.page.locator('#onsite-yes').is_checked())
        generator.answer.assert_not_called()

    def test_lebenslauf_picker_selects_first_and_never_asks_ai(self):
        for selected in [0, 1]:
            with self.subTest(selected=selected):
                cards = ''.join(f'''<div><div role="button"><p>PDF</p><p>candidate.pdf</p><p>{date}</p></div>
                    <div role="radio" aria-label="candidate.pdf"><input id="cv{i}" name="generated" type="radio" {'checked' if i == selected else ''}><label for="cv{i}"></label></div></div>'''
                    for i, date in enumerate(['10/12/2026', '8/31/2026']))
                self.page.set_content(f'''<section role="dialog"><h2>Apply to Example</h2>
                    <div><p>Lebenslauf*</p><p>Select or upload a resume in DOC, DOCX, or PDF format that is less than 2MB</p>
                    <fieldset role="radiogroup">{cards}</fieldset></div>
                    <fieldset><legend>Do you need sponsorship?*</legend><label><input id="sponsor" name="sponsor" type="radio">Yes</label><label><input name="sponsor" type="radio">No</label></fieldset>
                    <fieldset><legend>Anschreiben</legend><label><input id="letter" name="letter" type="radio">letter.pdf</label></fieldset>
                    </section>''')
                generator = Mock()
                self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
                self.assertTrue(self.page.locator('#cv0').is_checked())
                self.assertFalse(self.page.locator('#cv1').is_checked())
                self.assertTrue(self.page.locator('#sponsor').is_checked())
                self.assertFalse(self.page.locator('#letter').is_checked())
                self.assertEqual(self.page.locator('[data-ea-resume-choice]').count(), 2)
                generator.answer.assert_not_called()

    def test_localized_cv_picker_uses_names_or_upload_instructions_without_ai(self):
        for heading, help_text in [
            ('Curriculum*', ''), ('Currículum*', ''), ('Currículo*', ''),
            ('Curriculum vitae*', ''), ('Curri\u0301culum*', ''),
            ('Application document*', 'Select or upload a resume in DOC, DOCX, or PDF format that is less than 2MB'),
        ]:
            with self.subTest(heading=heading):
                self.page.set_content(f'''<section role="dialog"><h2>Apply to Example</h2>
                    <div><p>{heading}</p><p>{help_text}</p><fieldset role="radiogroup">
                    <div><div role="button">candidate.pdf</div><div role="radio" aria-label="candidate.pdf"><input id="first" type="radio" name="generated"><label for="first"></label></div></div>
                    <div><div role="button">older.pdf</div><div role="radio" aria-label="older.pdf"><input id="second" type="radio" name="generated" checked><label for="second"></label></div></div>
                    </fieldset></div></section>''')
                generator = Mock()
                self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
                self.assertTrue(self.page.locator('#first').is_checked())
                self.assertFalse(self.page.locator('#second').is_checked())
                self.assertEqual(self.page.locator('[data-ea-resume-choice]').count(), 2)
                generator.answer.assert_not_called()

    def test_optional_localized_cv_and_other_documents_stay_separate(self):
        self.page.set_content('''<section role="dialog"><h2>Apply to Example</h2>
            <div><p>Currículum</p><p>Select or upload a resume in DOC, DOCX, or PDF format</p>
            <fieldset><label><input id="cv" name="cv" type="radio">candidate.pdf</label></fieldset></div>
            <fieldset><legend>Portfolio*</legend><label><input id="portfolio" name="portfolio" type="radio">portfolio.pdf</label></fieldset>
            <fieldset><legend>Cover letter*</legend><label><input id="cover" name="cover" type="radio">letter.pdf</label></fieldset>
            </section>''')
        missing = fill_form(self.page.get_by_role('dialog'), self.settings)
        self.assertEqual(set(missing), {'Portfolio*', 'Cover letter*'})
        self.assertEqual(self.page.locator('input:checked').count(), 0)

    @patch('easy_apply.ai.AnswerGenerator.answer')
    def test_curriculum_application_advances_without_ai(self, answer):
        self.settings.ai_enabled = True
        self.context.unroute('**/*')
        start, end = JOB.index('<label for="email">'), JOB.index('<button type="submit">Next')
        content = JOB[:start] + '''<div><p>Currículum*</p><p>Select or upload a resume in DOC, DOCX, or PDF format</p>
            <fieldset><label><input name="documents" type="radio" checked>candidate.pdf</label>
            <label><input name="documents" type="radio">older.pdf</label></fieldset></div>''' + JOB[end:]
        content = content.replace('<p>Review your application</p>', '<p>Review your application</p><div><p>Currículum*</p><p>candidate.pdf</p></div>')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')
        answer.assert_not_called()

    @patch('easy_apply.ai.AnswerGenerator.answer')
    def test_lebenslauf_full_flow_and_review_do_not_call_model(self, answer):
        self.settings.ai_enabled = True
        self.context.unroute('**/*')
        start = JOB.index('<label for="email">')
        end = JOB.index('<button type="submit">Next')
        content = JOB[:start] + '''<p>Lebenslauf*</p><fieldset role="radiogroup">
            <label><input type="radio" name="documents" required checked>candidate.pdf</label>
            <label><input type="radio" name="documents" required>older.pdf</label></fieldset>''' + JOB[end:]
        content = content.replace('<p>Review your application</p>', '<p>Review your application</p><div><p>Lebenslauf*</p><p>candidate.pdf</p></div>')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')
        answer.assert_not_called()

    def test_optional_lebenslauf_skips_but_missing_required_document_blocks(self):
        self.page.set_content('''<section role="dialog"><p>Lebenslauf</p>
            <fieldset><label><input id="first" name="documents" type="radio">first.pdf</label>
            <label><input id="second" name="documents" type="radio" checked>second.pdf</label></fieldset></section>''')
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertTrue(self.page.locator('#second').is_checked())
        self.page.set_content('<section role="dialog"><p>Lebenslauf*</p><label for="upload">Lebenslauf*</label><input id="upload" type="file" required></section>')
        self.assertIn('Could not identify your first uploaded resume card', fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator))
        generator.answer.assert_not_called()

    def test_resume_review_summary_does_not_block_final_submission(self):
        self.context.unroute('**/*')
        content = JOB.replace('<p>Review your application</p>', '<h2>Review your application</h2><div><h3>Resume</h3><p>candidate.pdf</p></div>')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_resume_review_without_document_still_requires_attention(self):
        self.page.set_content('<section role="dialog"><h2>Review your application</h2><div><h3>Resume*</h3></div></section>')
        self.assertIn('Could not identify your first uploaded resume card', fill_form(self.page.get_by_role('dialog'), self.settings))

    def test_cv_custom_radio_selection_is_verified(self):
        self.page.set_content('''<section role="dialog"><h2>Your resume*</h2>
            <div role="radio" id="first" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">first.pdf</div>
            <div role="radio" aria-checked="false">second.pdf</div></section>''')
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings), [])
        self.assertEqual(self.page.locator('#first').get_attribute('aria-checked'), 'true')
        self.page.locator('#first').evaluate("e => { e.setAttribute('aria-checked','false'); e.onclick=null; }")
        self.assertIn('Could not verify selection', ' '.join(fill_form(self.page.get_by_role('dialog'), self.settings)))

    def test_cv_detection_preserves_unknown_questions_and_cover_letter_choices(self):
        self.page.set_content('''<section role="dialog"><h2>CV*</h2>
            <div><input id="first" type="radio"><span>first.pdf</span></div>
            <fieldset aria-required="true"><legend>Cover letter</legend><label><input id="cover" name="cover" type="radio">letter.pdf</label></fieldset>
            <div><label><input id="unknown" name="question" type="radio" required>Yes</label></div></section>''')
        missing = fill_form(self.page.get_by_role('dialog'), self.settings)
        self.assertIn('Cover letter', missing)
        self.assertIn('Unlabelled radio question', missing)
        self.assertTrue(self.page.locator('#first').is_checked())
        self.assertFalse(self.page.locator('#cover').is_checked())
        self.assertFalse(self.page.locator('#unknown').is_checked())

    @patch('easy_apply.ai.post_response')
    def test_cv_only_application_advances_and_submits_without_ai(self, post):
        self.settings.ai_enabled = True
        self.context.unroute('**/*')
        start = JOB.index('<label for="email">')
        end = JOB.index('<button type="submit">Next')
        content = JOB[:start] + '''<h3>Resume</h3>
            <div><input type="radio" name="cv" required><span>first.pdf</span></div>
            <div><input type="radio" name="cv" required><span>second.pdf</span></div>''' + JOB[end:]
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')
        post.assert_not_called()

    def test_unknown_text_calls_ai_with_job_and_field_limit(self):
        self.page.set_content('''<section role="dialog"><label for="q">Describe relevant experience *</label><textarea id="q" required maxlength="200"></textarea></section>''')
        generator = Mock()
        generator.answer.return_value = "I build ML systems with Python."
        job = {"title": "ML Engineer", "description": "Python models"}
        missing = fill_form(self.page.get_by_role("dialog"), self.settings, generator=generator, job=job)
        self.assertEqual(missing, [])
        generator.answer.assert_called_once_with(job, "Describe relevant experience *", "textarea", [], 200)
        self.assertEqual(self.page.locator("#q").input_value(), "I build ML systems with Python.")

    @patch('easy_apply.ai.generate_local')
    def test_capula_motivation_is_generated_checked_and_filled(self, local):
        from easy_apply.ai import AnswerGenerator
        from tests.test_ai import completed, reviewed
        label = ('In 300 words or less, please state why you are interested in this role at Capula '
                 'and what you think you would bring to the organisation in terms of skill set:')
        self.page.set_content(f'<section role="dialog"><label for="q">{label}</label><textarea id="q" required></textarea></section>')
        self.settings.cv_text = 'Built ML systems with Python.'
        self.settings.ai_provider = 'ollama'
        local.side_effect = [completed('I would bring Python ML experience to quantitative research.'), reviewed()]
        generator = AnswerGenerator(self.settings, audit_path=Path(self.temp.name) / 'ai.jsonl')
        missing = fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator,
                            job={'title': 'Credit Quantitative Analyst', 'company': 'Capula'})
        self.assertEqual(missing, [])
        self.assertEqual(self.page.locator('#q').input_value(), 'I would bring Python ML experience to quantitative research.')
        self.assertEqual(local.call_count, 2)

    def test_salary_uses_accessible_counter_limit_without_ai(self):
        self.page.set_content('''<section role="dialog"><input id="salary" required aria-label="What are your salary expectations?" aria-describedby="salary-info">
            <div id="salary-info"><p><span aria-hidden="true">0/20</span><span>0 of 20 characters</span></p></div>
            <input id="other" aria-label="Photo" aria-describedby="other-info"><div id="other-info">0/5</div></section>''')
        self.settings.answers['What are your salary expectations?'] = 'At least GBP 75,000 per year.'
        generator = Mock()
        self.assertEqual(fill_form(self.page.get_by_role('dialog'), self.settings, generator=generator), [])
        self.assertEqual(self.page.locator('#salary').input_value(), '75000')
        generator.answer.assert_not_called()

    def test_saved_answer_is_not_silently_cut_by_native_maxlength(self):
        self.page.set_content('<section role="dialog"><input id="q" required aria-label="Project details" maxlength="5"></section>')
        self.settings.answers['Project details'] = 'Important longer answer'
        missing = fill_form(self.page.get_by_role('dialog'), self.settings)
        self.assertIn('5-character limit', missing[0])
        self.assertEqual(self.page.locator('#q').input_value(), '')

    def test_plain_invalid_input_stops_repeated_clicks_and_continues_queue(self):
        self.store.add('https://www.linkedin.com/jobs/view/456/')
        clicks = []
        self.page.expose_function('recordNext', lambda: clicks.append(1))
        broken = JOB.replace('<button type="submit">Next</button>', '''<button type="button" onclick="window.recordNext();this.insertAdjacentHTML('beforebegin','<p>Invalid input</p>')">Next</button>''')
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=broken if '/123/' in route.request.url else JOB))
        self.driver.run(self.settings, self.store)
        rows = {r['id']:r for r in self.store.rows()}
        self.assertEqual(clicks, [1])
        self.assertEqual(rows['123']['status'], 'needs_input')
        self.assertIn('Invalid input', rows['123']['note'])
        self.assertEqual(rows['456']['application_source'], 'app_confirmed')

    def test_step_wait_handles_delayed_render_and_detects_no_progress(self):
        self.page.set_content('''<section role="dialog"><input aria-label="Question one"><button onclick="window.clicks=(window.clicks||0)+1;setTimeout(()=>document.querySelector('input').setAttribute('aria-label','Question two'),900)">Next</button></section>''')
        scope = self.page.get_by_role('dialog')
        before = scope.evaluate(STEP_STATE)['key']
        scope.get_by_role('button').click()
        wait_for_step_change(self.page, before, timeout=3)
        self.assertEqual(self.page.evaluate('window.clicks'), 1)
        before = scope.evaluate(STEP_STATE)['key']
        with self.assertRaisesRegex(NavigationError, 'did not advance'):
            wait_for_step_change(self.page, before, timeout=0.3)

    def test_modal_shell_waits_for_fields_and_footer_before_filling(self):
        self.context.unroute('**/*')
        content = JOB.replace('<form id="fields"', '<div id="loading"><button>Dismiss</button></div><div id="loaded" hidden><form id="fields"')
        content = content.replace('</section></main>', '</div></section></main>')
        content += '''<script>setTimeout(()=>{document.querySelector('#loaded').hidden=false;document.querySelector('#loading').remove()},1600)</script>'''
        self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertEqual(self.store.rows()[0]['application_source'], 'app_confirmed')

    def test_delayed_footer_is_found_and_loading_wait_is_bounded(self):
        self.page.set_content('''<dialog open><h2>Apply to Example</h2><button>Dismiss</button>
            <input aria-label="First name"><script>setTimeout(()=>document.querySelector('dialog').insertAdjacentHTML('beforeend','<button>Next</button>'),1500)</script></dialog>''')
        kind, _, label = prepare_action(self.page, self.page.locator('dialog'))
        self.assertEqual((kind, label), ('advance', 'Next'))
        self.page.set_content('<dialog open><h2>Apply to Example</h2><button>Dismiss</button></dialog>')
        with self.assertRaisesRegex(NavigationError, 'did not finish loading'):
            wait_for_form_ready(self.page, timeout=0.3)

    def test_required_city_typeahead_reads_paragraph_label_and_selects_saved_country(self):
        self.settings.city, self.settings.country = 'Reading', 'UK'
        self.page.set_content('''<dialog open><div componentkey="easyApplyFieldFocus_city"><p>Location (city)*</p>
            <div><input id="city" aria-autocomplete="list" oninput="document.querySelector('[role=listbox]').hidden=false"></div></div>
            <div role="listbox" hidden>
            <div role="option" onclick="document.querySelector('#city').value=this.innerText;this.parentElement.hidden=true">Reading, Pennsylvania, United States</div>
            <div role="option" onclick="document.querySelector('#city').value=this.innerText;this.parentElement.hidden=true">Reading, England, United Kingdom</div></div></dialog>''')
        self.assertEqual(fill_form(self.page.locator('dialog'), self.settings), [])
        self.assertEqual(self.page.locator('#city').input_value(), 'Reading, England, United Kingdom')

    def test_optional_city_typeahead_with_paragraph_label_is_ignored(self):
        self.settings.city = 'Reading'
        self.page.set_content('''<dialog open><div componentkey="easyApplyFieldFocus_city"><p>Location (city)</p>
            <div><input id="city" aria-autocomplete="list"></div></div></dialog>''')
        generator = Mock()
        self.assertEqual(fill_form(self.page.locator('dialog'), self.settings, generator=generator), [])
        self.assertEqual(self.page.locator('#city').input_value(), '')
        generator.answer.assert_not_called()

    def test_missing_metadata_can_continue_to_easy_apply(self):
        self.context.unroute("**/*")
        minimal = JOB.replace('<h1>Python Engineer</h1>', '').replace('job-details-jobs-unified-top-card__company-name', 'new-company').replace('job-details-jobs-unified-top-card__primary-description-container', 'new-location').replace('id="job-details"', 'id="changed-description"')
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=minimal))
        self.settings.allow_missing_metadata = True
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "applied")

    def test_structured_job_fallback(self):
        self.context.unroute("**/*")
        content = '''<html><script type="application/ld+json">{"@type":"JobPosting","identifier":{"value":"123"},"title":"ML Engineer","hiringOrganization":{"name":"Example"},"jobLocation":{"address":{"addressLocality":"London"}},"description":"<p>Python ML</p>"}</script><button>Easy Apply</button></html>'''
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        job = self.driver.read_job("https://www.linkedin.com/jobs/view/123/")
        self.assertEqual(job["title"], "ML Engineer")
        self.assertEqual(job["company"], "Example")
        self.assertEqual(job["description"], "Python ML")

    def test_short_viewport_nested_scroll_sibling_footer_and_apply_label(self):
        self.context.unroute("**/*")
        content = JOB.replace('<section role="dialog" hidden>', '<section class="artdeco-modal" hidden><div role="dialog">')
        content = content.replace("document.querySelector('[role=dialog]').hidden=false", "document.querySelector('.artdeco-modal').hidden=false")
        content = content.replace('</form>', '</form></div>')
        content = content.replace('<div id="review" hidden>', '<div id="review" hidden><div style="height:1400px">Long application review</div>')
        content = content.replace('type="submit">Next', 'type="submit" aria-label="Continue to next step">Next')
        content = content.replace('>Submit application</button>', '>Apply</button>')
        content = content.replace("document.querySelector('[role=dialog]').remove()", "document.querySelector('.artdeco-modal').remove()")
        content += '''<style>body{overflow:hidden}.artdeco-modal{position:fixed;top:12px;height:800px;overflow:hidden;width:500px;background:white}#review{height:180px;overflow-y:auto}</style>'''
        self.page.set_viewport_size({"width": 650, "height": 380})
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        self.driver.run(self.settings, self.store)
        self.assertEqual(self.page.evaluate("window.sends"), 1)
        self.assertEqual(self.store.rows()[0]["status"], "applied")

    def test_scrolling_reveals_lazy_footer_without_sending_trial_click(self):
        self.page.set_content('''<section role="dialog" style="height:120px;overflow:auto"><div style="height:900px">Review application</div></section>''')
        self.page.evaluate("""() => document.querySelector('[role=dialog]').addEventListener('scroll', event => {
            const root = event.currentTarget;
            if (!root.querySelector('button')) {
                const button = document.createElement('button');
                button.textContent = 'Submit application';
                button.onclick = () => window.sends = 1;
                root.append(button);
            }
        })""")
        scope = self.page.get_by_role("dialog")
        kind, button, _ = prepare_action(self.page, scope)
        self.assertEqual(kind, "submit")
        self.assertIsNone(self.page.evaluate("window.sends"))
        self.assertGreater(scope.evaluate("e=>e.scrollTop"), 0)
        button.click()
        self.assertEqual(self.page.evaluate("window.sends"), 1)

    def test_submit_label_with_company_and_hidden_duplicate(self):
        self.page.set_content('''<section role="dialog"><button hidden>Submit application</button><button aria-label="Submit application to Example Ltd" onclick="window.sends=1">Send application</button></section>''')
        kind, button, _ = prepare_action(self.page, self.page.get_by_role("dialog"))
        self.assertEqual(kind, "submit")
        self.assertEqual(button.inner_text(), "Send application")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_disabled_submit_never_claims_submission(self):
        self.context.unroute("**/*")
        content = JOB.replace('<button onclick="window.sends', '<button disabled onclick="window.sends')
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        with self.assertRaisesRegex(NeedsInput, "disabled"):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "needs_input")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_blocked_button_does_not_claim_or_force_submission(self):
        self.context.unroute("**/*")
        content = JOB.replace('<div id="review" hidden>', '<div id="review" hidden><div style="position:fixed;inset:0;z-index:999;background:white">Blocking popup</div>')
        self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=content))
        with self.assertRaisesRegex(NeedsInput, "Could not reach"):
            self.driver.run(self.settings, self.store)
        self.assertEqual(self.store.rows()[0]["status"], "needs_input")
        self.assertIsNone(self.page.evaluate("window.sends"))

    def test_unknown_blue_button_is_not_clicked(self):
        self.page.set_content('''<section role="dialog"><button style="background:blue" onclick="window.sends=1">Delete</button><button>Back</button></section>''')
        with self.assertRaisesRegex(NavigationError, "Delete, Back"):
            prepare_action(self.page, self.page.get_by_role("dialog"))
        self.assertIsNone(self.page.evaluate("window.sends"))


if __name__ == "__main__":
    unittest.main()

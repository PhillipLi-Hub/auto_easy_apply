import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from easy_apply.browser import LinkedInBrowser, Stopped
from easy_apply.models import Settings
from easy_apply.store import Store
from browser_tests.test_forms import JOB


def card(identifier, badge='', title='Python Engineer'):
    return f'<li class="job-card-container"><a href="/jobs/view/{identifier}/">{title}</a><span>{badge}</span></li>'


class SearchTests(unittest.TestCase):
    def test_recent_then_all_dates_preserves_duplicates_and_older_jobs(self):
        import json
        self.settings.newest_first = True
        self.settings.max_results = 1
        windows = []
        def respond(route):
            recent = parse_qs(urlparse(route.request.url).query).get('f_TPR') == ['r86400']
            windows.append(recent)
            content = card(456) if recent else card(456) + card(789)
            route.fulfill(content_type='text/html', body='<main><ul class="jobs-search-results-list">' + content + '</ul></main>')
        self.context.route('**/*', respond)
        self.assertEqual(self.driver.discover(self.settings, self.store), 2)
        self.assertEqual(windows, [True, False])
        rows = {r['id']: r for r in self.store.rows('queued')}
        self.assertEqual(set(rows), {'456', '789'})
        self.assertTrue(json.loads(rows['456']['snapshot'])['search_listing']['recent_search_at'])
        self.assertNotIn('recent_search_at', json.loads(rows['789']['snapshot'])['search_listing'])

    def test_empty_recent_search_still_collects_older_jobs(self):
        self.settings.newest_first = True
        self.settings.max_results = 1
        def respond(route):
            recent = 'f_TPR' in parse_qs(urlparse(route.request.url).query)
            content = '<h2>No jobs found</h2>' if recent else '<ul class="jobs-search-results-list">' + card(789) + '</ul>'
            route.fulfill(content_type='text/html', body='<main>' + content + '</main>')
        self.context.route('**/*', respond)
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        self.assertEqual(self.store.rows('queued')[0]['id'], '789')

    def test_quant_first_within_each_search_window(self):
        self.settings.newest_first = self.settings.prioritize_quant = True
        with patch('easy_apply.browser.discover_jobs', return_value=0) as collect:
            self.driver.discover(self.settings, self.store)
        self.assertEqual([(c.args[1].keywords, c.kwargs['past_24_hours']) for c in collect.call_args_list],
                         [('quant OR quantitative', True), ('Python', True),
                          ('quant OR quantitative', False), ('Python', False)])

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)

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
        self.store = Store(Path(self.temp.name) / 'jobs.sqlite3')
        self.settings = Settings(first_name='Ada', last_name='Example', email='ada@example.com', keywords='Python', location='London', answers={'Do you need sponsorship?': 'Yes'})
        self.logs = []
        self.driver = LinkedInBrowser(threading.Event(), self.logs.append)
        self.driver.page = self.page

    def route_html(self, html):
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))

    def history(self, identifier, status):
        self.store.add(f'https://www.linkedin.com/jobs/view/{identifier}/')
        self.store.update(str(identifier), status, 'Preserved history')

    def test_existing_top_results_do_not_consume_new_job_limit(self):
        self.history(123, 'applied')
        self.history(124, 'needs_input')
        self.settings.max_results = 1
        self.route_html('<main><ul class="jobs-search-results-list">' + card(123) + card(124) + card(456) + '</ul></main>')
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        self.assertEqual([r['id'] for r in self.store.rows('queued')], ['456'])
        self.assertEqual([r['id'] for r in self.store.rows('needs_input')], ['124'])
        self.assertTrue(any('2 already in history' in line for line in self.logs))

    def test_auto_apply_reaches_new_job_beyond_five_applied_pages(self):
        from urllib.parse import urlparse, parse_qs
        self.settings.max_results = self.settings.max_applications = 1
        def respond(route):
            if '/jobs/view/' in route.request.url:
                content = JOB
            else:
                number = int(parse_qs(urlparse(route.request.url).query).get('page', ['1'])[0])
                if number == 6:
                    content = '<main>' + card(456) + '</main>'
                else:
                    content = '<main>' + card(100 + number, 'Applied') + (
                        f'<nav class="artdeco-pagination"><button aria-label="View next page" '
                        f'onclick="location.href=\'/jobs/search/?page={number + 1}\'">Next</button></nav></main>')
            route.fulfill(content_type='text/html', body=content)
        self.context.route('**/*', respond)
        self.driver.run(self.settings, self.store)
        row = next(r for r in self.store.rows() if r['id'] == '456')
        self.assertEqual(row['application_source'], 'app_confirmed')
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertTrue(any('Search page 6:' in message for message in self.logs))

    def test_applied_card_badges_are_recorded_without_using_scan_limit(self):
        self.history(124, 'unverified')
        self.settings.max_results = 1
        self.route_html('<main><ul class="jobs-search-results-list">' + card(123, 'Applied') + card(124, 'Applied') + card(456) + '</ul></main>')
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        self.assertEqual([r['id'] for r in self.store.rows('applied')], ['123'])
        self.assertEqual([r['id'] for r in self.store.rows('unverified')], ['124'])
        self.assertEqual([r['id'] for r in self.store.rows('queued')], ['456'])

    def test_applied_first_page_advances_and_applies_new_job_on_second_page(self):
        self.settings.max_results = self.settings.max_applications = 1
        visited = []
        def respond(route):
            url = route.request.url
            visited.append(url)
            if '/jobs/view/' in url:
                content = JOB
            elif 'page=2' in url:
                content = '<main><ul class="jobs-search-results-list">' + card(456) + '</ul></main>'
            else:
                content = '<main><ul class="jobs-search-results-list">' + card(123, 'Applied') + card(124, 'Applied yesterday') + '''</ul>
                <nav class="artdeco-pagination"><button aria-label="View next page" onclick="location.href='/jobs/search/?page=2'">Next</button></nav></main>'''
            route.fulfill(content_type='text/html', body=content)
        self.context.route('**/*', respond)
        self.driver.run(self.settings, self.store)
        self.assertEqual({r['id'] for r in self.store.rows('applied')}, {'123', '124', '456'})
        self.assertEqual([url for url in visited if '/jobs/view/' in url], ['https://www.linkedin.com/jobs/view/456/'])
        self.assertEqual(self.page.evaluate('window.sends'), 1)
        self.assertTrue(any('Search page 2' in line for line in self.logs))

    def test_scrolls_nested_results_and_waits_for_delayed_virtualized_cards(self):
        self.history(123, 'applied')
        self.history(124, 'applied')
        self.settings.max_results = 1
        content = '''<main><div class="scaffold-layout__list"><ul id="scroll" style="height:100px;overflow:auto">
        ''' + card(123) + card(124) + '''</ul></div></main><style>li {min-height:100px}</style>
        <script>document.querySelector('#scroll').addEventListener('scroll', e => {
          if (window.loading) return;
          window.loading = true;
          setTimeout(() => e.target.innerHTML = '<li class="job-card-container"><a href="/jobs/view/456/">New role</a></li>', 900);
        });</script>'''
        self.route_html(content)
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        self.assertEqual([r['id'] for r in self.store.rows('queued')], ['456'])

    def test_hidden_badges_and_applied_scientist_title_are_not_applied_status(self):
        self.settings.max_results = 2
        content = '<main><ul class="jobs-search-results-list">' + card(123, '<span hidden>Applied</span>', 'Applied Scientist') + card(456) + '</ul><aside>' + card(789) + '</aside></main>'
        self.route_html(content)
        self.assertEqual(self.driver.discover(self.settings, self.store), 2)
        self.assertEqual({r['id'] for r in self.store.rows('queued')}, {'123', '456'})
        self.assertEqual(self.store.rows('applied'), [])

    def test_cards_with_job_ids_work_without_job_view_links(self):
        self.settings.max_results = 1
        self.route_html('<main><div class="jobs-search-results-list"><div data-occludable-job-id="123"><h3>Python Engineer</h3></div></div></main>')
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        self.assertEqual(self.store.rows()[0]['url'], 'https://www.linkedin.com/jobs/view/123/')

    def test_disabled_next_finishes_when_every_result_is_known(self):
        self.history(123, 'applied')
        self.route_html('<main><ul class="jobs-search-results-list">' + card(123) + '</ul><nav class="artdeco-pagination"><button disabled>Next</button></nav></main>')
        self.assertEqual(self.driver.discover(self.settings, self.store), 0)
        self.assertEqual(self.store.rows('queued'), [])
        self.assertTrue(any('No further results page available' in line for line in self.logs))

    def test_modern_component_cards_persist_metadata_and_applied_status(self):
        self.settings.max_results = 1
        self.route_html('''<main componentkey="SearchResultsMainContent">
          <div role="button" componentkey="job-card-component-ref-123"><p>Selected, Python Engineer (Verified job)</p><p>Example Ltd</p><p>London</p><p>Applied</p></div>
          <div role="button" componentkey="job-card-component-ref-456"><p><span>Data Scientist</span><span aria-hidden="true">Data Scientist</span></p><p>Science Ltd</p><p>London (Hybrid)</p></div>
          <section><a href="/jobs/view/999/">Selected detail, not a result card</a></section></main>''')
        self.assertEqual(self.driver.discover(self.settings, self.store), 1)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(set(rows), {'123', '456'})
        self.assertEqual(rows['123']['status'], 'applied')
        self.assertEqual(rows['123']['application_source'], 'linkedin_history')
        self.assertEqual(rows['123']['title'], 'Python Engineer')
        self.assertEqual(rows['456']['title'], 'Data Scientist')
        self.assertEqual(rows['456']['company'], 'Science Ltd')

    def test_full_collection_ignores_small_batch_cap_and_uses_modern_pagination(self):
        self.settings.max_results = 1
        def respond(route):
            if 'page=2' in route.request.url:
                content = '<main>' + card(789) + '</main>'
            else:
                content = '<main>' + card(123) + card(456) + '''<button data-testid="pagination-controls-next-button-visible" onclick="location.href='/jobs/search-results/?page=2'">Next</button></main>'''
            route.fulfill(content_type='text/html', body=content)
        self.context.route('**/*', respond)
        self.assertEqual(self.driver.discover(self.settings, self.store, full_scan=True), 3)
        self.assertEqual(len(self.store.rows('queued')), 3)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM scans').fetchone()[0], 'finished')

    def test_delayed_card_metadata_is_saved_after_id_is_seen(self):
        self.route_html('''<main><div role="button" componentkey="job-card-component-ref-123"></div></main>
          <script>setTimeout(()=>document.querySelector('[role=button]').innerHTML='<p>Data Scientist</p><p>Example Ltd</p><p>London</p><p>Applied</p>',500)</script>''')
        self.driver.discover(self.settings, self.store, full_scan=True)
        row = self.store.rows()[0]
        self.assertEqual(row['title'], 'Data Scientist')
        self.assertEqual(row['company'], 'Example Ltd')
        self.assertEqual(row['status'], 'applied')

    def test_no_results_is_recorded_as_exhausted(self):
        self.route_html('<main><h2>No results found</h2></main>')
        self.assertEqual(self.driver.discover(self.settings, self.store, full_scan=True), 0)
        with self.store.connect() as db:
            status, details = db.execute('SELECT status,details FROM scans').fetchone()
        self.assertEqual(status, 'finished')
        self.assertIn('"exhausted": true', details)

    def test_stop_preserves_already_collected_rows_and_scan_audit(self):
        self.route_html('<main>' + card(123) + '</main>')
        def check():
            if self.store.rows():
                raise Stopped('Stopped by user')
        self.driver.check = check
        with self.assertRaises(Stopped):
            self.driver.discover(self.settings, self.store, full_scan=True)
        self.assertEqual(self.store.rows()[0]['id'], '123')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM scans').fetchone()[0], 'stopped')


if __name__ == '__main__':
    unittest.main()

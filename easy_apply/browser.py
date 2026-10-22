"""Visible-browser automation with job-local recovery and protected submissions."""

import re
import math
from dataclasses import replace
from .priority import queue_order
import time
from urllib.parse import urlparse

from .models import DATA, job_id, normalize
from .ai import AnswerError, AnswerGenerator
from .page_data import APPLIED_STATUS, CLOSED_STATUS, SNAPSHOT, RESUME_NAMES, RESUME_REQUIRED, first_uploaded_resume
from .navigation import NavigationError, application_scope, prepare_action, STEP_STATE, wait_for_step_change, wait_for_form_ready, easy_apply_buttons
from .limits import ApplicationLimit, DailyLimit, RateLimited, visible_limit
from .search import discover_jobs
from .phone import phone_kind, uk_subscriber, uk_code_option, fill_uk_number
from .answer_format import format_answer
from .seniority import seniority_rejection


class NeedsInput(Exception):
    pass


class DailyLimitReached(NeedsInput):
    def __init__(self, linkedin_message, confirmed_in_run, job_id=None):
        self.linkedin_message = linkedin_message
        self.confirmed_in_run = confirmed_in_run
        self.job_id = job_id
        super().__init__("Stopped: LinkedIn's daily application maximum was reached. Remaining jobs are saved for a later run. " + linkedin_message)


class JobBlocked(NeedsInput):
    """A job-specific obstacle before Submit; other queued jobs can continue."""


class QuestionBlocked(JobBlocked):
    """An unanswered question before Submit; other queued jobs can continue."""


class Stopped(Exception):
    pass


FIELD_INFO = r"""el => {
  const shown = node => !!(node && (node.getClientRects().length || (node.labels && [...node.labels].some(x => x.getClientRects().length)) || node.closest('[role="checkbox"]')?.getClientRects().length));
  const group = el.closest('fieldset, [role="radiogroup"], .fb-dash-form-element');
  const refs = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ');
  const ownLabel = [...(el.labels || [])].map(x => x.innerText).join(' ').trim();
  const legend = group?.querySelector('legend, [data-test-form-element-label], .fb-dash-form-element__label')?.innerText || '';
  const field = el.closest('[componentkey^="easyApplyFieldFocus"]');
  const fieldLabel = field?.querySelector(':scope > p, :scope > label')?.innerText || '';
  const type = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
  let radioQuestion = legend || group?.getAttribute('aria-label') || '';
  if (type === 'radio' && group && !radioQuestion) {
    radioQuestion = (group.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
    // New layout: a question paragraph followed by a fieldset with no legend.
    for (let node = group.parentElement; node && !radioQuestion; node = node.parentElement) {
      const groups = [...node.querySelectorAll('fieldset, [role="radiogroup"]')]
        .filter(e => !e.parentElement?.closest('fieldset, [role="radiogroup"]'));
      if (groups.length > 1) break;
      const preceding = [...node.querySelectorAll('p, label, [role="heading"]')]
        .filter(e => !group.contains(e) && (e.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING)
          && e.innerText?.trim() && e.innerText.length < 600);
      radioQuestion = (preceding.filter(e => e.innerText.includes('*')).at(-1) || preceding.at(-1))?.innerText.trim() || '';
      if (node.matches('dialog, [role="dialog"]')) break;
    }
  }
  const question = type === 'radio' ? radioQuestion : (el.getAttribute('aria-label') || refs || ownLabel || legend || fieldLabel);
  // New radios have empty native labels; the visible Yes/No text is on the wrapper.
  const radioWrapper = type === 'radio' ? el.closest('[role="radio"]') : null;
  // SDUI also renders a native input with an empty label, followed by a
  // sibling paragraph. Its aria-label repeats the QUESTION, not the option.
  let nearbyOption = '';
  if (type === 'radio' && !ownLabel && !radioWrapper) {
    for (let node = el.parentElement; node && node !== group; node = node.parentElement) {
      if (node.matches('fieldset, [role="radiogroup"], dialog, [role="dialog"]') ||
          node.querySelectorAll('input[type="radio"]').length !== 1) break;
      const text = (node.innerText || '').trim();
      if (text && text.length <= 200 && text !== question.trim()) {
        nearbyOption = text;
        break;
      }
    }
  }
  const option = ownLabel || (radioWrapper?.innerText || '').trim() || nearbyOption || el.getAttribute('aria-label') || refs;
  // SDUI inputs often omit maxlength and expose only an accessible counter.
  const limits = el.maxLength > 0 ? [el.maxLength] : [];
  for (const id of (el.getAttribute('aria-describedby') || '').split(/\s+/)) {
    const helper = document.getElementById(id);
    if (!helper) continue;
    for (const node of [helper, ...helper.querySelectorAll('*')]) {
      const text = (node.innerText || '').trim();
      const match = text.match(/^\d+\s*\/\s*(\d+)$/) || text.match(/^\d+\s+of\s+(\d+)\s+characters$/i);
      if (match && Number(match[1]) > 0) limits.push(Number(match[1]));
    }
  }
  return {type, label: question.trim(), option, name: el.name,
    visible: shown(el), disabled: el.disabled || el.readOnly,
    required: !!(el.required || el.getAttribute('aria-required') === 'true' || group?.getAttribute('aria-required') === 'true' || question.includes('*') || ownLabel.includes('*') || refs.includes('*')),
    resumeChoice: !!el.closest('[data-ea-resume-card]') || el.hasAttribute('data-ea-resume-choice'),
    maxlength: limits.length ? Math.min(...limits) : null,
    inputmode: el.getAttribute('inputmode') || '',
    typeahead: el.getAttribute('aria-autocomplete') === 'list',
    placeholder: el.getAttribute('placeholder') || '', autocomplete: el.getAttribute('autocomplete') || '',
    checked: el.checked, value: el.value};
}"""


def set_checkbox(control, checked, check):
    """Use the visible native input, ARIA wrapper or associated label.

    LinkedIn review pages hide the input and render a separate clickable label.
    Always verify the native state; never force a click or set it with script.
    """
    from playwright.sync_api import Error as BrowserError

    check()
    try:
        if control.is_checked() == checked:
            return True
        if control.is_visible():
            control.set_checked(checked, timeout=3000)
        else:
            handle = control.evaluate_handle('''el => {
                const visible = e => e && e.checkVisibility({checkVisibilityCSS:true, checkOpacity:true});
                const wrapper = el.closest('[role="checkbox"]');
                if (visible(wrapper)) return wrapper;
                return [...(el.labels || [])].find(visible) || null;
            }''')
            try:
                target = handle.as_element()
                if target is None:
                    return False
                target.click(timeout=3000)
            finally:
                handle.dispose()
        check()
        return control.is_checked() == checked
    except BrowserError:
        return False


def fill_form(dialog, settings, check=lambda: None, generator=None, job=None, unresolved=None):
    """Fill required fields only, using saved facts before CV-grounded AI."""
    missing, radio_groups, question_specs = [], {}, {}
    resume_required = dialog.evaluate(RESUME_REQUIRED)
    if settings.resume_source == "linkedin_first" and resume_required:
        resume_step, selected, detail = first_uploaded_resume(dialog, check)
        if resume_step and not selected:
            missing.append(detail)

    def resolve(label, kind, options=None, maxlength=None):
        answer = settings.answer(label) if label else None
        if label:
            question_specs[label] = {'label': label, 'kind': kind, 'options': options or [],
                                     'maxlength': maxlength, 'previous_answer': answer, 'source': 'form'}
        if answer is None and generator and kind != "checkbox" and label:
            try:
                answer = generator.answer(job or {}, label, kind, options, maxlength)
                check()
            except AnswerError as exc:
                missing.append(str(exc))
        return answer
    controls = dialog.locator("input, select, textarea")
    # Select the dial code before filling any phone number, regardless of DOM order.
    subscriber = uk_subscriber(settings.phone, settings.country)
    separate_code = False
    handled_codes = []
    for index, control in enumerate(controls.all()):
        check()
        info = control.evaluate(FIELD_INFO)
        if not info['visible'] or phone_kind(info) != 'code' or not subscriber:
            continue
        if not info['required']:
            if info['type'] == 'select':
                selected = control.locator('option:checked').all_text_contents()
                selected = ' '.join(selected)
                separate_code = separate_code or bool(re.search(r'\+44\b', selected))
            else:
                separate_code = separate_code or control.input_value() in {'+44', '44', '0044'}
            continue
        handled_codes.append(index)
        if info['type'] == 'select':
            options = control.locator('option').evaluate_all('els => els.map(e => ({label:e.label,value:e.value,disabled:e.disabled}))')
            option = uk_code_option(options, settings.answer(info['label']))
            if option and (not info['disabled'] or control.input_value() == option['value']):
                if not info['disabled']:
                    control.select_option(value=option['value'])
                separate_code = True
            else:
                missing.append(f"{info['label']}: could not select the UK (+44) country code")
        elif info['type'] in {'text', 'tel', 'number'}:
            if not info['disabled']:
                control.fill('44' if info['type'] == 'number' else '+44')
            if re.sub(r'\D', '', control.input_value()) in {'44', '0044'}:
                separate_code = True
            else:
                missing.append(f"{info['label']}: country code must be +44")
        else:
            missing.append(f"{info['label']}: unsupported country code control")
    for index, control in enumerate(controls.all()):
        check()
        info = control.evaluate(FIELD_INFO)
        kind, label = info["type"], info["label"]
        if index in handled_codes:
            continue
        if info["disabled"] or info["resumeChoice"] or kind in {"hidden", "submit", "button", "reset"}:
            continue
        if kind != 'radio' and not info['required']:
            # Skip even saved optional answers. Do not ask AI about photos,
            # locations, cover letters or other optional form controls.
            if kind == 'checkbox' and info['visible'] and info['checked']:
                if not set_checkbox(control, False, check):
                    missing.append(f"{label or 'Optional checkbox'}: could not verify it was unticked")
            continue
        if kind == "file":
            if re.search(r'\b' + RESUME_NAMES + r'\b', label, re.I):
                if settings.resume_source == "linkedin_first":
                    continue
                elif not settings.resume:
                    missing.append("Choose your resume file in Profile")
                else:
                    control.set_input_files(settings.resume)
            elif info["required"]:
                missing.append(label or "Unlabelled required file upload")
            continue
        if not info["visible"]:
            continue
        contact_kind = phone_kind(info)
        if contact_kind == 'number' and subscriber and kind in {'text', 'tel', 'number'}:
            # A saved phone answer can override the profile number, but must still
            # be formatted using the code field and HTML constraints.
            supplied = settings.answer(label) or settings.phone
            number = uk_subscriber(supplied, 'UK')
            if not number or not fill_uk_number(control, number, separate_code, info):
                missing.append(f"{label}: phone format was not accepted; check this field")
            continue
        if contact_kind and not subscriber and settings.answer(label) is None:
            missing.append(f"{label}: enter your phone with its international country code in Profile")
            continue
        if kind == "radio":
            key = (info["name"], label)
            radio_groups.setdefault(key, []).append((control, info))
            continue
        options = []
        if kind == "select":
            options = control.locator("option").evaluate_all("els => els.filter(e => !e.disabled && e.value).map(e => e.label)")
        answer = resolve(label, kind, options, info["maxlength"])
        if kind == "checkbox":
            if answer is not None:
                if normalize(answer) not in {"yes", "no", "true", "false"}:
                    missing.append(f"{label}: checkbox answer must be yes or no")
                else:
                    if not set_checkbox(control, normalize(answer) in {"yes", "true"}, check):
                        missing.append(f"{label or 'Required checkbox'}: could not verify the saved answer was selected")
            elif info["required"]:
                missing.append(label or "Unlabelled required consent")
            continue
        if answer is None:
            # Do not trust prefilled required answers from a previous application.
            if info["required"]:
                missing.append(label or "Unlabelled required question")
            continue
        if kind == "select":
            options = control.locator("option").evaluate_all("els => els.map(e => ({label:e.label,value:e.value,disabled:e.disabled}))")
            matches = [x for x in options if not x["disabled"] and normalize(x["label"]) == normalize(answer)]
            if len(matches) != 1:
                missing.append(f"{label}: saved answer does not match exactly one option")
            else:
                control.select_option(value=matches[0]["value"])
        elif kind in {"text", "email", "tel", "url", "number", "textarea", "date"}:
            try:
                answer = format_answer(answer, info)
            except ValueError as exc:
                missing.append(f"{label}: {exc}")
                continue
            control.fill(answer)
            if info['typeahead']:
                if normalize(label) in {'location (city)', 'city', 'city of residence', 'city or location'}:
                    if not select_city_suggestion(control, answer, settings.country, check):
                        missing.append(f"{label}: could not identify one matching city and country suggestion")
                else:
                    missing.append(f"{label}: choose an autocomplete suggestion manually")
                continue
            if control.input_value() != answer:
                missing.append(f"{label}: the form changed or truncated the answer")
        else:
            missing.append(f"{label or 'Question'}: unsupported control type {kind}")
    for (_, label), choices in radio_groups.items():
        check()
        # required on any member makes the entire radio group required, allowing
        # the correct option even when only a different member has the attribute.
        if not any(info['required'] for _, info in choices):
            continue
        answer = resolve(label, "radio", [info["option"] for _, info in choices])
        matches = [(c, i) for c, i in choices if answer is not None and normalize(i["option"]) == normalize(answer)]
        if len(matches) != 1:
            missing.append(label or "Unlabelled radio question")
        else:
            choice = matches[0][0]
            if not set_radio(choice, check):
                missing.append(f'{label}: could not verify the selected radio answer')
    for editor in dialog.locator('[contenteditable="true"]').all():
        info = editor.evaluate(FIELD_INFO)
        if info['visible'] and not info['disabled'] and info['required']:
            missing.append("Unsupported required rich-text question; complete manually")
    if unresolved is not None:
        for label, spec in question_specs.items():
            issues = [issue for issue in missing if issue == label or issue.startswith(label + ':') or issue.endswith(': ' + label)]
            if issues:
                unresolved.append(spec | {'issue': ' | '.join(issues)})
    return list(dict.fromkeys(missing))


def set_radio(control, check):
    """Click a visible label/wrapper for hidden SDUI radios and verify selection."""
    from playwright.sync_api import Error as BrowserError

    check()
    try:
        if control.is_checked():
            return True
        handle = control.evaluate_handle('''el => {
            const visible = e => e && e.checkVisibility({checkVisibilityCSS:true, checkOpacity:true});
            const wrapper = el.closest('[role="radio"]');
            return (visible(wrapper) && wrapper) || [...(el.labels || [])].find(visible) ||
                (visible(el) ? el : null);
        }''')
        try:
            target = handle.as_element()
            if target is None:
                return False
            target.click(timeout=3000)
        finally:
            handle.dispose()
        check()
        return control.is_checked()
    except BrowserError:
        return False


def select_city_suggestion(control, city, country, check):
    """Choose a suggestion for the saved residence, never a similarly named city abroad."""
    country = {'uk': 'united kingdom', 'gb': 'united kingdom', 'gbr': 'united kingdom'}.get(normalize(country), normalize(country))
    for _ in range(20):
        check()
        matches = []
        for option in control.page.locator('[role="listbox"] [role="option"]').all():
            if not option.is_visible():
                continue
            text = normalize(option.inner_text())
            if text == normalize(city) or (country and text.startswith(normalize(city) + ',') and text.endswith(', ' + country)):
                matches.append((option, text))
        names = {text for _, text in matches}
        if len(names) == 1:
            option, text = matches[0]
            option.click()
            return normalize(control.input_value()) == text
        if len(names) > 1:
            return False
        control.page.wait_for_timeout(200)
    return False


class LinkedInBrowser:
    def __init__(self, stop_event, log, ai_connection=lambda state, detail: None, cooldown=lambda seconds: None):
        self.stop_event, self.log = stop_event, log
        self.ai_connection = ai_connection
        self.cooldown = cooldown
        self.playwright = self.context = self.page = None
        self.generator = None
        self.operation = 'checking the browser'

    def check(self, check_limits=True):
        if self.stop_event.is_set():
            raise Stopped("Stopped by user")
        if self.page and not self.page.is_closed():
            current = urlparse(self.page.url)
            if current.hostname not in {"www.linkedin.com", "linkedin.com", None}:
                raise NeedsInput("Browser left LinkedIn; external applications are not automated")
            if any(part in current.path for part in ["/checkpoint", "/challenge", "/login", "/authwall"]):
                raise NeedsInput("Complete LinkedIn login or verification manually, then restart the run")
            if self.page.locator('iframe[src*="captcha"], iframe[title*="challenge" i]').count():
                raise NeedsInput("Verification challenge detected; complete it manually")
            if check_limits:
                limit = visible_limit(self.page)
                if limit:
                    raise limit

    def pause_for_limit(self, limit):
        deadline = time.monotonic() + limit.seconds
        self.log(f"Paused: LinkedIn says you are applying too fast. Waiting {limit.seconds / 60:g} minutes. Stop remains available.")
        self.cooldown(limit.seconds)
        last_minute = None
        last_second = limit.seconds
        while time.monotonic() < deadline:
            self.check(check_limits=False)
            current = visible_limit(self.page)
            if isinstance(current, DailyLimit):
                raise current
            remaining = max(0, deadline - time.monotonic())
            minute = int(remaining / 60)
            if minute != last_minute:
                self.log(f"Cooldown: {int(remaining + 0.999)} seconds remaining.")
                last_minute = minute
                # Restore the detailed status after the minute-level activity log.
                last_second = None
            second = math.ceil(remaining)
            if second != last_second:
                self.cooldown(second)
                last_second = second
            self.stop_event.wait(min(1, remaining))
        self.check(check_limits=False)
        self.log("Cooldown finished. Checking LinkedIn before continuing.")
        self.cooldown(0)

    def open(self):
        if self.context and self.page and not self.page.is_closed():
            self.page.bring_to_front()
            return
        if self.playwright:
            self.close()
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            str(DATA / "browser_profile"), channel="msedge", headless=False,
            viewport={"width": 1280, "height": 900}, accept_downloads=False)
        self.context.set_default_timeout(6000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        self.log("Browser open. Sign in manually in Edge, then return to the app.")

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()
            self.context = self.playwright = self.page = None

    def discover(self, settings, store, full_scan=False):
        self.operation = 'collecting job listings'
        searches = [settings]
        if settings.newest_first and settings.prioritize_quant and settings.keywords != 'quant OR quantitative':
            searches.insert(0, replace(settings, keywords='quant OR quantitative'))
        count = 0
        # The recent filter is only a first pass, never a permanent restriction.
        for recent in ([True, False] if settings.newest_first else [False]):
            for search in searches:
                self.check()
                self.log(f"Searching {'last 24 hours' if recent else 'all dates (older jobs remain eligible)'}: {search.keywords}")
                count += discover_jobs(self.page, search, store, self.check, self.log, full_scan, past_24_hours=recent)
        excluded = store.exclude_ineligible_jobs(settings)
        if excluded:
            self.log(f'Removed {excluded} jobs excluded by role level, clearance or employment preferences from pending applications.')
        return count

    def text(self, selector):
        locator = self.page.locator(selector).first
        return locator.inner_text().strip() if locator.count() else ""

    def read_job(self, url):
        self.operation = 'loading the job page'
        self.check()
        self.page.goto(url, wait_until="domcontentloaded")
        self.operation = 'reading job details'
        # A DOMContentLoaded event does not mean the client-rendered job is ready.
        job = {}
        for _ in range(40):
            self.check()
            job = self.page.evaluate(SNAPSHOT)
            job["applied_status"] = self.page.evaluate(APPLIED_STATUS)
            job["closed_status"] = self.page.evaluate(CLOSED_STATUS)
            if job["applied_status"] or job["closed_status"]:
                break
            if easy_apply_buttons(self.page):
                if job.get("title") and job.get("description"):
                    break
                # Give metadata a little longer, but its absence is not a blocker.
                if _ >= 3:
                    break
            self.page.wait_for_timeout(500)
        self.check()
        if job_id(self.page.url) != job_id(url):
            raise JobBlocked("Loaded job does not match queued job ID")
        job["url"] = url
        return job

    def record_previous_application(self, identifier, job, store):
        status = job.get("applied_status") or self.page.evaluate(APPLIED_STATUS)
        if not status:
            return False
        job["applied_status"] = status
        store.update(identifier, "applied", f"Already applied on LinkedIn: {status}. No new submission.", job, source='linkedin_history')
        self.log(f"Already applied: {job.get('title') or identifier}. Continuing to the next queued job.")
        return True

    def wait_for_easy_apply(self, identifier, job, store, timeout=12):
        from playwright.sync_api import TimeoutError as BrowserTimeout

        deadline = time.monotonic() + timeout
        seen = False
        while time.monotonic() < deadline:
            self.check()
            if self.record_previous_application(identifier, job, store) or self.record_closed_job(identifier, job, store):
                return None
            for button in easy_apply_buttons(self.page):
                seen = True
                try:
                    if not button.is_enabled(timeout=500):
                        continue
                    # Trial checks visibility, overlays and reachability without
                    # opening an application. Re-query after transient re-renders.
                    button.click(trial=True, timeout=1000)
                    self.check()
                    return button
                except BrowserTimeout:
                    continue
            self.page.wait_for_timeout(250)
        self.check()
        if self.record_previous_application(identifier, job, store) or self.record_closed_job(identifier, job, store):
            return None
        if seen:
            raise JobBlocked('Easy Apply is visible but disabled or covered. No application was opened; check the browser before retrying.')
        raise JobBlocked('No visible Easy Apply button after waiting for the job page to finish loading. This job can be retried; check availability and sign-in.')

    def apply(self, row, settings, store):
        identifier = row["id"]
        # Known excluded titles need no browser visit. Check live titles again
        # below for previously missing or stale metadata.
        rejection = seniority_rejection(row) if settings.exclude_senior_roles else None
        if rejection and row.get('status') in {'queued', 'needs_input', 'error'}:
            store.update(identifier, 'skipped', rejection)
            self.log(f"Skipped {row['title']}: {rejection}")
            return False
        job = self.read_job(row["url"])
        # Search listings can supply metadata absent from SDUI's job page.
        # read_job already verified this page has the queued job ID.
        for key in ('title', 'company'):
            if not job.get(key) and row.get(key):
                job[key] = row[key]
        if self.record_previous_application(identifier, job, store):
            return False
        if self.record_closed_job(identifier, job, store):
            return False
        rejection = settings.rejection(job)
        if rejection:
            store.update(identifier, "skipped", rejection, job)
            self.log(f"Skipped {job['title'] or identifier}: {rejection}")
            return False
        unknown = [key for key in ["title", "company", "location", "description"] if not job.get(key)]
        note = "Proceeding with unreadable metadata: " + ", ".join(unknown) if unknown else "Filters matched"
        store.update(identifier, "queued", note, job)
        if unknown:
            self.log(note + "; known filter mismatches still exclude jobs")
        self.operation = 'finding an available Easy Apply button'
        button = self.wait_for_easy_apply(identifier, job, store)
        if button is None:
            return False
        self.check()
        self.operation = 'opening the application'
        button.click()
        for _ in range(15):
            self.operation = 'reading the application form'
            self.check()
            try:
                dialog = wait_for_form_ready(self.page, self.check)
            except NavigationError as exc:
                raise QuestionBlocked(str(exc)) from exc
            unresolved = []
            self.operation = 'filling required application fields'
            missing = fill_form(dialog, settings, self.check, self.generator, job, unresolved)
            if missing:
                store.record_questions(identifier, unresolved)
                raise QuestionBlocked("Save exact answers or resolve these fields: " + " | ".join(missing))
            self.page.wait_for_timeout(400)
            self.operation = 'finding the next application action'
            try:
                action, button, label = prepare_action(self.page, dialog, self.check)
            except NavigationError as exc:
                raise JobBlocked(str(exc)) from exc
            self.log(f"Application action ready: {label}")
            if action == "submit":
                self.check()
                if not store.claim(identifier):
                    raise NeedsInput("This application is already submitted or awaiting verification")
                # Any exception after this point must leave a non-retryable state.
                try:
                    self.check()
                    self.operation = 'submitting the application'
                    button.click(timeout=6000)
                    self.operation = 'verifying submission confirmation'
                    confirmation = self.page.get_by_text(re.compile(r"^(?:Application sent|Application submitted|Your application was sent(?: to .*)?)[.!]?$", re.I)).first
                    deadline = time.monotonic() + 10
                    while not confirmation.is_visible():
                        self.check()
                        if time.monotonic() >= deadline:
                            raise NeedsInput("Timed out waiting for submission confirmation")
                        self.page.wait_for_timeout(200)
                except ApplicationLimit:
                    store.update(identifier, "unverified", "LinkedIn displayed an application limit after Submit; verify before any retry", job)
                    raise
                except Stopped:
                    store.update(identifier, "unverified", "Stopped after Submit; verify before any retry", job)
                    raise
                except Exception:
                    store.update(identifier, "unverified", "Submit was attempted; verify in LinkedIn before any retry")
                    raise NeedsInput("Submission confirmation was not verified. Run stopped to prevent duplicates.") from None
                store.update(identifier, "applied", "LinkedIn displayed an application-sent confirmation", job, source='app_confirmed')
                self.log(f"Applied: {job['title']} — {job['company']}")
                return True
            self.check()
            previous_step = dialog.evaluate(STEP_STATE)['key']
            self.operation = 'advancing to the next application step'
            button.click(timeout=6000)
            try:
                wait_for_step_change(self.page, previous_step, self.check)
            except NavigationError as exc:
                raise QuestionBlocked(str(exc)) from exc
        raise QuestionBlocked("Application exceeded 15 distinct steps; inspect manually")

    def record_closed_job(self, identifier, job, store):
        status = job.get('closed_status') or self.page.evaluate(CLOSED_STATUS)
        if not status:
            return False
        job['closed_status'] = status
        store.update(identifier, 'skipped', f"Closed listing: {status}", job)
        self.log(f"Skipped closed job {job.get('title') or identifier}: {status}. Continuing to the next queued job.")
        return True

    def run(self, settings, store):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        settings.validate(for_run=True)
        self.generator = AnswerGenerator(settings, self.log, self.check, connection=self.ai_connection) if settings.ai_enabled else None
        count = 0
        scanned = False
        finish_reason = ''
        cooldowns = 0
        row = None
        while settings.run_until_daily_limit or count < settings.max_applications:
            try:
                self.operation = 'checking the browser and queue'
                self.check()
                if settings.newest_first and not scanned:
                    self.log('Refreshing last-24-hours listings, then all dates, before processing the saved queue…')
                    self.discover(settings, store)
                    scanned = True
                if row:
                    current = next(x for x in store.rows() if x["id"] == row["id"])
                    if current["status"] == "unverified":
                        job = self.read_job(row["url"])
                        if self.record_previous_application(row["id"], job, store):
                            store.update(row["id"], "applied", "LinkedIn confirmed the submitted application after cooldown", job, source='app_confirmed')
                            self.log(f"Verified submission after cooldown: {row['id']}")
                            count += 1
                            row = None
                            continue
                        raise NeedsInput("Cooldown finished, but the last submission is unverified. Check it in LinkedIn before continuing.")
                excluded = store.exclude_ineligible_jobs(settings)
                if excluded:
                    self.log(f'Skipped {excluded} jobs excluded by role level, clearance or employment preferences before processing the queue.')
                queued = store.rows("queued")
                includes = [normalize(term) for term in settings.title_include.split(',') if term.strip()]
                if includes:
                    matches = lambda item: any(term in normalize(item['title']) for term in includes)
                    # Old searches can leave hundreds of unrelated queued jobs.
                    # Keep those for another search instead of opening them all
                    # before refreshing the current search. Unknown titles still
                    # receive a live check after the one bounded search refresh.
                    queued = [item for item in queued if not item['title'].strip() or matches(item)]
                    queued.sort(key=lambda item: not matches(item))
                    if not scanned and not any(matches(item) for item in queued):
                        self.log('No queued titles match the current filters. Searching for more matching roles…')
                        self.discover(settings, store)
                        scanned = True
                        continue
                if not queued:
                    if scanned:
                        finish_reason = 'No queued jobs match the current title filters after searching.'
                        self.log("No more queued jobs match this search. Other saved jobs remain queued; adjust filters or Collect jobs to database to search again.")
                        break
                    self.log("Queue empty. Finding more jobs with your saved filters…")
                    self.discover(settings, store)
                    scanned = True
                    continue
                if settings.prioritize_quant or settings.newest_first:
                    queued.sort(key=lambda item: queue_order(item, settings))
                row = queued[0]
                if settings.prioritize_quant or settings.newest_first:
                    self.log(f"Next by priority: {row['title'] or row['id']} · " +
                             (f"listing date {row['posted_at']}" if row.get('posted_at') else
                              f"first discovered {row['first_seen']}" if row.get('first_seen') else 'listing date unknown'))
                progress = f'{count} new applications; continuing to daily limit' if settings.run_until_daily_limit else f'{count}/{settings.max_applications} new applications'
                self.log(f"Processing job {row['id']} · {progress} · {len(queued)} queued")
                count += bool(self.apply(row, settings, store))
                row = None
            except DailyLimit as exc:
                if row:
                    current = next(x for x in store.rows() if x["id"] == row["id"])
                    if current["status"] == "queued":
                        store.update(row["id"], "queued", "Daily application limit reached; left for a later run")
                raise DailyLimitReached(str(exc), count, row['id'] if row else None) from None
            except RateLimited as exc:
                cooldowns += 1
                if cooldowns > 2 and not settings.run_until_daily_limit:
                    raise NeedsInput("Stopped: LinkedIn still reports an application rate limit after two pauses. Try again later.") from None
                try:
                    self.pause_for_limit(exc)
                    # Reload only after the full cooldown. A pending submission
                    # is verified, never clicked again automatically.
                    self.page.reload(wait_until="domcontentloaded")
                except DailyLimit as daily:
                    raise DailyLimitReached(str(daily), count, row['id'] if row else None) from None
                except RateLimited:
                    # The next loop checks the still-visible warning and pauses
                    # again, subject to the bounded cooldown count above.
                    pass
                continue
            except Stopped:
                raise
            except JobBlocked as exc:
                # No submission has been attempted on this path. The next
                # loop checks login and platform limits before navigating.
                current = next((x for x in store.rows() if row and x['id'] == row['id']), None)
                if not current or current['status'] in {'applied', 'unverified', 'submitting'}:
                    raise
                # The next loop checks global restrictions before navigating.
                store.update(row['id'], 'needs_input', str(exc))
                self.log(f"Needs attention: {row['id']} — {exc}. Continuing to the next queued job.")
                row = None
                continue
            except NeedsInput as exc:
                if row:
                    current = next(x for x in store.rows() if x["id"] == row["id"])
                    if current["status"] not in {"applied", "unverified", "submitting"}:
                        store.update(row["id"], "needs_input", str(exc))
                raise
            except Exception as exc:
                # Keep context without persisting form values or credentials
                # that may be present in Playwright's full call log.
                method = re.search(r'\b(?:Page|Locator|BrowserContext)\.[A-Za-z_]+', str(exc))
                detail = f"{type(exc).__name__} while {self.operation}"
                if method:
                    detail += f" ({method.group(0)})"
                if row:
                    current = next(x for x in store.rows() if x["id"] == row["id"])
                    status = "unverified" if current["status"] in {"submitting", "unverified"} else "applied" if current["status"] == "applied" else "error"
                    store.update(row["id"], status, detail + '; inspect before retrying')
                    if (isinstance(exc, BrowserTimeout) and status == 'error'
                            and self.operation != 'checking the browser and queue'):
                        self.log(f"Needs attention: {row['id']} — {detail}. Continuing to the next queued job.")
                        row = None
                        # The loop checks login, verification and limits before
                        # navigating. Never retry a possibly submitted form.
                        continue
                raise NeedsInput(f"Browser interaction failed: {detail}. Inspect the open browser and job note before retrying.") from exc
        if not settings.run_until_daily_limit and count >= settings.max_applications:
            self.log(f"Configured run limit reached ({settings.max_applications}). Remaining jobs stay queued.")
            finish_reason = f'Run limit reached ({settings.max_applications}).'
        self.log(f"Run finished: {count} confirmed applications. {finish_reason}")

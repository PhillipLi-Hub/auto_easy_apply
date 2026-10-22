"""Find application actions inside the active modal and scroll before clicking."""

import re
import time
import uuid


class NavigationError(Exception):
    pass


def easy_apply_buttons(page):
    """Recognize the current posting's entry control by visible or ARIA text.

    SDUI uses data-view-name=job-apply-button and can replace that control
    while the page hydrates. Return fresh locators on every polling pass.
    """
    token = uuid.uuid4().hex
    # Inspect in a single DOM operation. Iterating nth() locators can wait on
    # vanished indices or resolve to another control when hydration replaces
    # the page. A private marker binds each returned locator to that element.
    matches = page.locator('button, [role="button"], a[href]').evaluate_all(r'''(elements, token) => {
        const eligible = el => {
          const excluded = '#job-details, .jobs-description, .jobs-description__content, [data-test-job-description], aside, .job-card-container, [componentkey^="job-card-component-ref-"], .jobs-search-results-list, .jobs-similar-jobs, [role="dialog"], dialog';
          if (el.closest(excluded) || !el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) return false;
          // LinkedIn also renders its entry action as a same-job anchor.
          // Do not follow external application or other posting links.
          if (el.tagName === 'A') {
            const current = location.pathname.match(/^\/jobs\/view\/(\d+)\/?$/)?.[1];
            let target;
            try { target = new URL(el.getAttribute('href'), location.href); }
            catch { return false; }
            if (!current || target.protocol !== 'https:' ||
                !/^(?:www\.)?linkedin\.com$/i.test(target.hostname) ||
                target.pathname.match(/^\/jobs\/view\/(\d+)\/?$/)?.[1] !== current) return false;
          }
          const primary = el.closest('[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]');
          if (primary) {
            const boundary = [...primary.querySelectorAll('h2, [role="heading"]')].find(h => /^(?:About the job|More jobs|Similar jobs|Recommended jobs)$/i.test(h.innerText.trim()));
            if (boundary && !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
          }
          const copy = el.cloneNode(true);
          copy.querySelectorAll('svg, img, [aria-hidden="true"]').forEach(n => n.remove());
          const names = [el.getAttribute('aria-label'), copy.textContent,
            (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ')];
          return names.some(name => /^easy\s+apply(?:\s+to\b.*)?$/i.test((name || '').trim().replace(/\s+/g, ' ')));
        };
        return elements.flatMap((el, index) => {
          if (!eligible(el)) return [];
          const key = token + '-' + index;
          el.setAttribute('data-easy-apply-entry', key);
          return [{key, preferred:el.getAttribute('data-view-name') === 'job-apply-button'}];
        });
    }''', token)
    # Prefer LinkedIn's explicit entry marker over generic text controls.
    matches.sort(key=lambda item: not item['preferred'])
    return [page.locator(f'[data-easy-apply-entry="{item["key"]}"]') for item in matches]


STEP_STATE = r'''root => {
  const shown = e => e.checkVisibility() || [...(e.labels || [])].some(l => l.checkVisibility());
  const controls = [...root.querySelectorAll('input,select,textarea,[contenteditable="true"]')]
    .filter(e => shown(e) && !['hidden','submit','button'].includes(e.type));
  const questions = controls.map(e => [e.tagName,e.type,
    e.getAttribute('aria-label') || [...(e.labels || [])].map(l=>l.innerText).join(' '),
    e.closest('fieldset')?.querySelector('legend')?.innerText || '']);
  const headings = [...root.querySelectorAll('h1,h2,h3,h4,[role=heading]')].filter(shown).map(e=>e.innerText);
  const text = root.innerText;
  const progress = text.match(/\b\d+\s*\/\s*\d+\s*pages?\b/i)?.[0] || '';
  const review = /review (?:your )?application/i.test(text);
  // Work/education steps can contain only LinkedIn profile summaries and an
  // Add more button. They are complete pages, not empty loading shells.
  const profileSummary = !!progress && (
    /\bYour title\b/i.test(text) && /\bCompany\b/i.test(text) && /\bDates of employment\b/i.test(text) ||
    /\bSchool\b/i.test(text) && /\bDegree\b/i.test(text) && /\bField of study\b/i.test(text));
  const buttons = [...root.querySelectorAll('button,[role=button]')].filter(shown).map(e=>e.innerText || e.getAttribute('aria-label'));
  return {key:JSON.stringify([questions,headings,progress,review,buttons]),
    ready:buttons.length > 0 && (controls.length > 0 || review || profileSummary)};
}'''

VALIDATION_MESSAGES = r'''root => {
  const messages = [];
  for (const e of root.querySelectorAll('[role=alert], .artdeco-inline-feedback--error, p, span')) {
    if (!e.checkVisibility()) continue;
    const text = (e.innerText || '').trim();
    if (text && (e.matches('[role=alert], .artdeco-inline-feedback--error') ||
        /^(?:invalid input|please enter a valid .+|this field is required|required field)[.!]?$/i.test(text))) messages.push(text);
  }
  for (const e of root.querySelectorAll('input,textarea,select')) {
    if (e.disabled || !(e.checkVisibility() || [...(e.labels || [])].some(l=>l.checkVisibility()))) continue;
    if (e.getAttribute('aria-invalid') === 'true' || (e.willValidate && !e.validity.valid)) {
      const label = e.getAttribute('aria-label') || [...(e.labels || [])].map(l=>l.innerText).join(' ') || 'Required field';
      messages.push(label + ': invalid input');
    }
  }
  return [...new Set(messages)];
}'''


def wait_for_step_change(page, previous, check=lambda: None, timeout=8):
    """Wait for a stable new step; never repeatedly click a rejected Next."""
    deadline = time.monotonic() + timeout
    candidate, stable_since = None, None
    while time.monotonic() < deadline:
        check()
        scope = application_scope(page, check)
        state = scope.evaluate(STEP_STATE)
        if state['key'] != previous and state['ready']:
            if candidate != state['key']:
                candidate, stable_since = state['key'], time.monotonic()
            elif time.monotonic() - stable_since >= 0.6:
                return
        else:
            candidate = stable_since = None
            messages = scope.evaluate(VALIDATION_MESSAGES)
            if messages:
                raise NavigationError('Form validation: ' + ' | '.join(messages))
        page.wait_for_timeout(200)
    raise NavigationError('The application did not advance after Next/Review. Check its required fields; no repeated click was sent.')


SUBMIT = re.compile(r"^(?:submit(?: (?:your )?application)?|send application|apply)(?:\s+to\s+.+)?$", re.I)
ADVANCE = re.compile(r"^(?:next|continue|review|review (?:your )?application|continue to (?:the )?next step|proceed to (?:the )?next step)$", re.I)

SCROLL_MODAL = """root => {
  // The document body is normally locked while LinkedIn's modal is open.
  // Scroll the actual modal/panels, not the job page behind it.
  const nodes = [root, ...root.querySelectorAll('*')].filter(el => {
    if (el.matches('textarea, select, input, [role=listbox]')) return false;
    const style = getComputedStyle(el);
    return el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2 &&
      /auto|scroll/.test(style.overflowY);
  });
  for (const el of nodes.reverse()) el.scrollTo({top:el.scrollHeight, behavior:'instant'});
  const rect = root.getBoundingClientRect();
  // Recover a fixed modal clipped by a short viewport; never change field or
  // button state. This adjustment remains scoped to the application modal.
  if (rect.height > innerHeight - Math.max(rect.top, 0) - 8) {
    root.style.maxHeight = Math.max(100, innerHeight - Math.max(rect.top, 0) - 12) + 'px';
    root.style.overflowY = 'auto';
    root.scrollTo({top:root.scrollHeight, behavior:'instant'});
  }
}"""


def application_scope(page, check=lambda: None):
    for _ in range(30):
        check()
        candidates = [el for el in page.locator('.artdeco-modal, [role="dialog"], dialog[open]').all() if el.is_visible()]
        for el in candidates:
            name = el.get_attribute("aria-label") or ""
            if re.search(r"apply|application", name, re.I) or el.get_by_role("heading", name=re.compile(r"Apply to|Easy Apply|application", re.I)).count():
                return el
        if len(candidates) == 1:
            return candidates[0]
        page.wait_for_timeout(150)
    raise NavigationError("Could not identify the visible application window")


def action_buttons(scope):
    actions, labels = [], []
    for button in scope.get_by_role("button").all():
        if not button.is_visible():
            continue
        info = button.evaluate("""el => ({
          text:(el.innerText || el.value || '').trim().replace(/\\s+/g,' '),
          aria:(el.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' '),
          submit:el.hasAttribute('data-easy-apply-submit-button')
        })""")
        names = [name for name in [info["aria"], info["text"]] if name]
        label = info["text"] or info["aria"] or "unlabelled button"
        labels.append(label)
        final = info["submit"] or any(SUBMIT.fullmatch(name) for name in names)
        advance = any(ADVANCE.fullmatch(name) for name in names)
        if final and advance:
            raise NavigationError("Application button has conflicting submit/next labels; inspect manually")
        if final or advance:
            actions.append(("submit" if final else "advance", button, label))
    return actions, labels


def wait_for_form_ready(page, check=lambda: None, timeout=15):
    """A modal shell with only Dismiss is still loading, not a form step."""
    deadline = time.monotonic() + timeout
    candidate, stable_since = None, None
    while time.monotonic() < deadline:
        check()
        scope = application_scope(page, check)
        state = scope.evaluate(STEP_STATE)
        actions, _ = action_buttons(scope)
        if actions and state['ready']:
            if candidate != state['key']:
                candidate, stable_since = state['key'], time.monotonic()
            elif time.monotonic() - stable_since >= 0.4:
                return scope
        else:
            candidate = stable_since = None
        page.wait_for_timeout(200)
    raise NavigationError('Application form did not finish loading: no ready fields and Next/Review/Submit action. Try this job again later.')


def prepare_action(page, scope, check=lambda: None):
    """Resolve and trial-click an action without sending it or claiming submission."""
    from playwright.sync_api import TimeoutError as BrowserTimeout

    labels = []
    deadline = time.monotonic() + 12
    disabled_checks = 0
    while time.monotonic() < deadline:
        check()
        scope.evaluate(SCROLL_MODAL)
        page.wait_for_timeout(150)
        actions, labels = action_buttons(scope)
        if actions:
            kinds = {kind for kind, _, _ in actions}
            if len(kinds) != 1:
                raise NavigationError("Both next-step and final-submit actions are visible; inspect the application")
            enabled = [item for item in actions if item[1].is_enabled()]
            if not enabled:
                disabled_checks += 1
                if disabled_checks < 6:
                    continue
                raise NavigationError("Application action is disabled. Check required fields, consent and form validation.")
            kind, button, label = enabled[0]
            check()
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.evaluate("el => el.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'})")
                button.click(trial=True, timeout=3000)
            except BrowserTimeout:
                raise NavigationError(f"Could not reach '{label}' inside the application window. A popup or overlay may be covering it; no click was sent.") from None
            check()
            return kind, button, label
    shown = ", ".join(labels[:10]) or "none"
    raise NavigationError("Unrecognized application step after scrolling. Visible buttons: " + shown)

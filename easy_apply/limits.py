"""Recognize visible application limits and respect the site's requested pause."""

import re


class ApplicationLimit(Exception):
    def __init__(self, message, seconds=600):
        super().__init__(message)
        self.seconds = seconds


class RateLimited(ApplicationLimit):
    pass


class DailyLimit(ApplicationLimit):
    pass


LIMIT_MESSAGES = r"""() => {
  const excluded = '#job-details, .jobs-description, .jobs-description__content, [data-test-job-description], aside, .job-card-container, [componentkey^="job-card-component-ref-"], input, textarea, select';
  const selectors = '[role="alert"], [role="alertdialog"], [role="heading"], [aria-live="polite"], [aria-live="assertive"], .artdeco-inline-feedback, .artdeco-toast-item, [role="dialog"], dialog, [aria-modal="true"], .artdeco-modal, main, h1, h2, h3';
  const messages = new Set();
  for (const root of document.querySelectorAll(selectors)) {
    for (const el of [root, ...root.querySelectorAll('h1,h2,h3,p,span,div,[role="heading"]')]) {
      if (el.closest(excluded) || el.querySelector(excluded) ||
          !el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) continue;
      // SDUI job descriptions/recommendations have no stable legacy classes.
      const primary = el.closest('[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]');
      if (primary && !el.closest('[role="dialog"], [role="alertdialog"], dialog, [aria-modal="true"], .artdeco-modal')) {
        const boundary = [...primary.querySelectorAll('h2,[role="heading"]')].find(h =>
          h.checkVisibility({checkVisibilityCSS:true}) && /^(?:About the job|More jobs|Similar jobs|Recommended jobs)$/i.test(h.innerText.trim()));
        if (boundary && (el.contains(boundary) || !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING))) continue;
      }
      const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (text && text.length <= 1000) messages.add(text);
    }
  }
  return [...messages];
}"""


def classify_limit(messages):
    # Daily limits take priority even when a temporary warning is also visible.
    for text in messages:
        value = text.casefold().replace('’', "'")
        if (re.search(r'\b(?:daily|today|per day|24 hours)\b', value)
                and re.search(r'\b(?:limit|maximum)\b', value)
                and re.search(r'\b(?:reach\w*|exceed\w*|maximum|cannot|can.t)\b', value)
                and re.search(r'\b(?:appl\w*|submi\w*)\b', value)):
            return DailyLimit(text)
    for text in messages:
        value = text.casefold().replace('’', "'")
        if (re.search(r'\bapplying too (?:fast|quickly)\b', value)
                or re.search(r'\btoo many applications\b', value)
                or re.search(r'\blimit how (?:often|quickly) you can apply\b', value)
                or re.search(r'\beasy apply is temporarily paused\b', value)
                or re.search(r"\b(?:you(?:'re| are)\s+)?applying at a fast pace\b", value)):
            seconds = 600
            for amount, unit in re.findall(r'\b(\d+)\s*(seconds?|minutes?|hours?)\b', value):
                seconds = max(seconds, int(amount) * (3600 if unit.startswith('hour') else 60 if unit.startswith('minute') else 1))
            return RateLimited(text, seconds)
    return None


def cooldown_status(seconds):
    seconds = max(0, int(seconds))
    if seconds == 0:
        return 'Cooldown finished — checking LinkedIn before continuing.'
    minutes, seconds = divmod(seconds, 60)
    return f'LinkedIn temporarily paused Easy Apply — wait {minutes:02d}:{seconds:02d} before checking again. Stop remains available.'


def visible_limit(page):
    return classify_limit(page.evaluate(LIMIT_MESSAGES))

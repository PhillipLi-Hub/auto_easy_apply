"""Read rendered job cards and advance the visible search results."""

import re
import json
from datetime import datetime, timezone

from .models import job_id


RESULT_ROOTS = '.jobs-search-results-list, .scaffold-layout__list, .jobs-search-results__list, [data-test-job-results], [componentkey="SearchResultsMainContent"]'
CARDS = '[componentkey^="job-card-component-ref-"], [data-occludable-job-id], [data-job-id], .job-card-container, .jobs-search-results__list-item, li'

READ_RESULTS = r"""({roots, cards}) => {
  const visible = el => el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
  const scopes = [...document.querySelectorAll(roots)].filter(visible);
  if (!scopes.length) scopes.push(document.querySelector('main') || document.body);
  const found = new Map();
  for (const scope of scopes) {
    const modernCards = scope.querySelectorAll('[role="button"][componentkey^="job-card-component-ref-"]');
    const nodes = modernCards.length ? modernCards : scope.querySelectorAll('a[href*="/jobs/view/"], [data-occludable-job-id], [data-job-id]');
    for (const node of nodes) {
      if (!visible(node) || node.closest('#job-details, .jobs-description, aside')) continue;
      const card = node.closest(cards) || node;
      const link = node.matches('a') ? node : node.querySelector('a[href*="/jobs/view/"]');
      const id = node.getAttribute('data-occludable-job-id') || node.getAttribute('data-job-id') || node.getAttribute('componentkey')?.match(/^job-card-component-ref-(\d+)$/)?.[1];
      const url = link?.href || (/^\d+$/.test(id || '') ? 'https://www.linkedin.com/jobs/view/' + id + '/' : '');
      if (!url) continue;
      // A badge belongs to this single job card, not to the result-list wrapper.
      const ownIds = new Set([...card.querySelectorAll('a[href*="/jobs/view/"]')]
        .map(a => a.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)/)?.[1]).filter(Boolean));
      let applied = '';
      if (ownIds.size <= 1) {
        for (const el of [card, ...card.querySelectorAll('span,li,p,div')]) {
          if (!visible(el)) continue;
          const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
          if (/^(?:already applied|applied|application (?:sent|submitted))(?:[.!]|\s+(?:on\b|today\b|yesterday\b|\d)[^\n]*)?$/i.test(text)) {
            applied = text;
            break;
          }
        }
      }
      const previous = found.get(url);
      const text = selectors => {
        for (const el of card.querySelectorAll(selectors)) if (visible(el) && el.innerText.trim()) return el.innerText.trim();
        return '';
      };
      const paragraphs = id && node.hasAttribute('componentkey') ? [...card.querySelectorAll('p')].slice(0,3).map(el => {
        const copy = el.cloneNode(true);
        copy.querySelectorAll('[aria-hidden="true"]').forEach(e=>e.remove());
        return copy.textContent.trim().replace(/\s+/g,' ');
      }) : [];
      found.set(url, {url, applied: applied || previous?.applied || '',
        title: (paragraphs[0] || text('.job-card-list__title, .job-card-container__link, h3, [data-test-job-title]') || (link?.innerText || '').trim()).replace(/^Selected,\s*/i,'').replace(/\s*\(Verified job\)$/i,''),
        company: paragraphs[1] || text('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, [data-test-company-name]'),
        location: paragraphs[2] || text('.job-card-container__metadata-item, [data-test-job-location]'),
        employment_type: (card.innerText || '').split('\n').map(s => s.trim()).filter(s => /^(?:full[ -]time|part[ -]time|contract|temporary|permanent|fixed[ -]term|freelance|internship|apprenticeship)(?:\s*[/,|]\s*(?:full[ -]time|part[ -]time|contract|temporary|permanent))*$/i.test(s)).join(' / '),
        salary: text('.job-card-container__salary-info, [data-test-salary]'),
        posted_datetime: card.querySelector('time[datetime]')?.getAttribute('datetime') || '',
        posted_text: (card.innerText || '').match(/\b(?:Reposted\s+)?(?:\d+\+?\s+(?:minute|hour|day|week|month|year)s?\s+ago|just now|today)\b/i)?.[0] || ''});
    }
  }
  return [...found.values()];
}"""

SCROLL_RESULTS = r"""roots => {
  const scopes = [...document.querySelectorAll(roots)].filter(el => el.checkVisibility());
  const scope = scopes[0] || document.querySelector('main') || document.body;
  const first = scope.querySelector('[role="button"][componentkey^="job-card-component-ref-"]') || scope.querySelector('a[href*="/jobs/view/"], [data-occludable-job-id], [data-job-id]');
  const candidates = [];
  for (let el = first; el && el !== document.body; el = el.parentElement) candidates.push(el);
  candidates.push(scope, ...scope.querySelectorAll('*'));
  const scrollable = candidates.find(el => el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2 && /auto|scroll/.test(getComputedStyle(el).overflowY));
  const el = scrollable || document.scrollingElement;
  const before = el.scrollTop;
  // Incremental scrolling preserves cards in virtualized lists.
  el.scrollTop += Math.max(100, el.clientHeight * 0.8);
  return Math.abs(el.scrollTop - before) > 1;
}"""


def next_results_page(page, check):
    name = re.compile(r'^(?:next|next page|view next page)$', re.I)
    controls = page.get_by_role('button', name=name).all() + page.get_by_role('link', name=name).all()
    candidates = []
    for control in controls:
        if not control.is_visible() or not control.is_enabled() or control.get_attribute('aria-disabled') == 'true':
            continue
        label = control.get_attribute('aria-label') or control.inner_text().strip()
        in_pagination = control.evaluate("e => !!e.closest('.artdeco-pagination, .jobs-search-pagination, [aria-label*=pagination i], [data-test-pagination]')")
        if in_pagination or control.get_attribute('data-testid') == 'pagination-controls-next-button-visible' or re.fullmatch(r'(?:view )?next page', label, re.I):
            candidates.append(control)
    if not candidates:
        # Some layouts expose only numbered page buttons.
        current = page.locator('.artdeco-pagination [aria-current="true"], .artdeco-pagination [aria-current="page"], .jobs-search-pagination [aria-current="page"], [data-testid="pagination-controls-list"] [aria-current="true"]')
        for item in current.all():
            number = re.search(r'\d+', item.get_attribute('aria-label') or item.inner_text())
            if item.is_visible() and number:
                target = int(number[0]) + 1
                buttons = page.get_by_role('button', name=re.compile(rf'^(?:Page )?{target}$', re.I))
                candidates.extend(b for b in buttons.all() if b.is_visible() and b.is_enabled())
    if not candidates:
        return False
    check()
    candidates[0].scroll_into_view_if_needed()
    candidates[0].click()
    return True


def capture_diagnostics(page, directory):
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / 'latest-search.json'
    data = page.evaluate(r"""() => ({url:location.href, title:document.title,
      headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.innerText).filter(Boolean).slice(0,40),
      links:[...document.querySelectorAll('a[href]')].map(e=>({url:new URL(e.href).origin+new URL(e.href).pathname,text:e.innerText.slice(0,150),classes:e.className})).slice(0,500),
      markers:[...document.querySelectorAll('[data-job-id], [data-occludable-job-id], [componentkey^="job-card-component-ref-"]')].map(e=>({tag:e.tagName,id:e.getAttribute('data-job-id')||e.getAttribute('data-occludable-job-id'),component:e.getAttribute('componentkey'),classes:e.className})).slice(0,250)
    })""")
    data['captured'] = datetime.now(timezone.utc).isoformat()
    report.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return report


def discover_jobs(page, settings, store, check, log, full_scan=False, past_24_hours=False):
    scan_id = store.start_scan({'url': settings.search_url(past_24_hours), 'full_scan': full_scan})
    try:
        summary = _collect_jobs(page, settings, store, check, log, full_scan, past_24_hours)
        summary['full_scan'] = full_scan
        store.finish_scan(scan_id, 'finished' if summary['exhausted'] else 'partial', summary)
        return summary['added']
    except Exception as exc:
        details = {'error': str(exc), 'type': type(exc).__name__}
        try:
            details['diagnostics'] = str(capture_diagnostics(page, store.path.parent / 'diagnostics'))
            log('Search diagnostics saved: ' + details['diagnostics'])
        except Exception:
            pass
        store.finish_scan(scan_id, 'stopped', details)
        raise


def _collect_jobs(page, settings, store, check, log, full_scan, past_24_hours=False):
    check()
    page.goto(settings.search_url(past_24_hours), wait_until='domcontentloaded')
    recent_search_at = store.now() if past_24_hours else ''
    known = {row['id']: row['status'] for row in store.rows()}
    seen, fingerprints, listing_cache = set(), set(), {}
    added = applied = existing = 0
    # Known/applied jobs on early pages must not hide new roles farther down.
    # Normal runs still stop at their configured new-job limit.
    max_pages = 40
    limit = 5000 if full_scan else settings.max_results
    reason = f'Search page limit reached ({max_pages} pages)'
    for page_number in range(1, max_pages + 1):
        log(f'Search page {page_number}: collecting into the database; {added} new jobs saved.')
        idle = 0
        page_ids = set()
        for attempt in range(60):
            check()
            results = page.evaluate(READ_RESULTS, {'roots': RESULT_ROOTS, 'cards': CARDS})
            if not results and page.get_by_role('heading', name=re.compile(r'^No (?:results|jobs) found[.!]?$', re.I)).is_visible():
                break
            before = len(page_ids)
            for result in results:
                if recent_search_at:
                    result['recent_search_at'] = recent_search_at
                try:
                    identifier = job_id(result['url'])
                except ValueError:
                    continue
                page_ids.add(identifier)
                if identifier in seen:
                    # Virtualized cards often render their ID before their text.
                    # Refresh metadata on later passes, including applied badges.
                    if listing_cache.get(identifier) != result:
                        store.save_listing_metadata(identifier, result)
                        listing_cache[identifier] = result
                    if result['applied'] and known.get(identifier) not in {'applied', 'submitting', 'unverified'}:
                        store.update(identifier, 'applied', 'Search result shows already applied: ' + result['applied'], source='linkedin_history')
                        known[identifier] = 'applied'
                    continue
                seen.add(identifier)
                if result['applied']:
                    status = known.get(identifier)
                    if status is None:
                        store.add(result['url'])
                    # Preserve uncertain sends for explicit verification.
                    if status not in {'applied', 'submitting', 'unverified'}:
                        store.update(identifier, 'applied', 'Search result shows already applied: ' + result['applied'], {'title': result['title']}, source='linkedin_history')
                    known[identifier] = 'applied'
                    applied += 1
                    log(f'Already applied in search results: {identifier}; looking for another job.')
                elif identifier in known:
                    existing += 1
                elif store.add(result['url']):
                    known[identifier] = 'queued'
                    added += 1
                    log(f'Queued new job {identifier}: {result["title"] or "title unavailable"} ({added} saved).')
                store.save_listing_metadata(identifier, result)
                listing_cache[identifier] = result
                if added >= limit:
                    reason = 'New-job scan limit reached'
                    break
            if added >= limit:
                break
            moved = page.evaluate(SCROLL_RESULTS, RESULT_ROOTS)
            idle = idle + 1 if len(page_ids) == before and not moved else 0
            if idle >= (6 if page_ids else 40):
                break
            page.wait_for_timeout(250)
        if added >= limit:
            break
        if not page_ids:
            if page.get_by_role('heading', name=re.compile(r'^No (?:results|jobs) found[.!]?$', re.I)).count():
                reason = 'No more results found'
                break
            report = capture_diagnostics(page, store.path.parent / 'diagnostics')
            log(f'No job cards could be read on page {page_number}. Diagnostics saved: {report}')
            reason = 'No readable job cards; inspect search diagnostics'
            break
        fingerprint = tuple(sorted(page_ids))
        if fingerprint in fingerprints:
            reason = 'Results page repeated; no further progress'
            break
        fingerprints.add(fingerprint)
        if page_number == max_pages:
            break
        if not next_results_page(page, check):
            reason = 'No further results page available'
            break
        # Allow an asynchronous pagination click to replace the current cards.
        for _ in range(40):
            check()
            page.wait_for_timeout(250)
            next_ids = set()
            for item in page.evaluate(READ_RESULTS, {'roots': RESULT_ROOTS, 'cards': CARDS}):
                try:
                    next_ids.add(job_id(item['url']))
                except ValueError:
                    pass
            if next_ids and not next_ids.issubset(page_ids):
                break
    check()
    log(f'Search finished: {added} new jobs queued, {applied} already applied, {existing} already in history. {reason}.')
    capture_diagnostics(page, store.path.parent / 'diagnostics')
    return {'added': added, 'seen': len(seen), 'applied': applied, 'existing': existing,
            'pages': page_number, 'reason': reason, 'exhausted': reason in {'No further results page available', 'No more results found'}}

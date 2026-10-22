"""DOM fallbacks for changing LinkedIn layouts; no private API requests."""

APPLIED_STATUS = r"""() => {
  // Read status only in the current job's application area or header. Text in
  // descriptions, recommended jobs, and hidden templates is not application history.
  const roots = [...document.querySelectorAll([
    '.jobs-s-apply', '.jobs-apply-button--applied',
    '.jobs-s-apply__applied-date', '.jobs-s-apply__application-link',
    '.job-details-jobs-unified-top-card', '.jobs-unified-top-card',
    '.jobs-details-top-card', '[data-test-job-application-status]'
  ].join(','))];
  const excluded = '#job-details, .jobs-description, .jobs-description__content, [data-test-job-description], aside, .job-card-container, [componentkey^="job-card-component-ref-"], .jobs-search-results-list, .jobs-similar-jobs, [role="dialog"], dialog';
  const shown = el => el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
  // SDUI moves existing-application history out of the header into a separate
  // Application status card. Scope it to this job's Primary content and stop
  // at other headings, rather than searching the whole page for "submitted".
  for (const primary of document.querySelectorAll('[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]')) {
    const headings = [...primary.querySelectorAll('h2, [role="heading"]')].filter(shown);
    const boundary = headings.find(el => /^(?:About the job|More jobs|Similar jobs|Recommended jobs)$/i.test(el.innerText.trim()));
    for (const heading of headings) {
      if (heading.closest(excluded) || !/^Application status$/i.test(heading.innerText.trim())) continue;
      if (boundary && !(heading.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      for (let panel = heading.parentElement; panel && panel !== primary; panel = panel.parentElement) {
        if (panel.closest(excluded) || panel.querySelectorAll('h2, [role="heading"]').length !== 1) break;
        roots.push(panel);
      }
    }
  }
  const status = /^(?:(?:you(?:['’]ve| have)?\s+)?(?:already\s+)?applied(?:\s+(?:on\s+)?(?:\d[^\n]*|today|yesterday|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b[^\n]*))?|application\s+(?:sent|submitted)|your application was sent(?:\s+to\s+[^\n]+)?)[.!]?$/i;
  for (const root of roots) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el.closest(excluded) || !el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) continue;
      const text = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
      if (text && text.length <= 200 && status.test(text)) return text;
    }
  }
  return '';
}"""

CLOSED_STATUS = r"""() => {
  // Exact visible notices in the current job header/application area only.
  // SDUI uses both polite and assertive live regions in the job header.
  const roots = document.querySelectorAll([
    '.jobs-s-apply', '.job-details-jobs-unified-top-card',
    '.jobs-unified-top-card', '.jobs-details-top-card',
    '[data-test-job-application-status]', '.jobs-details-top-card__apply-error',
    '[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"] [aria-live="assertive"]',
    '[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"] [aria-live="polite"]'
  ].join(','));
  const excluded = '#job-details, .jobs-description, .jobs-description__content, [data-test-job-description], aside, .job-card-container, [componentkey^="job-card-component-ref-"], .jobs-search-results-list, .jobs-similar-jobs, [role="dialog"]';
  const closed = /^(?:(?:no longer|not currently|not) accepting applications|(?:this (?:job|position) is )?no longer available|(?:this (?:job|position) (?:has |is ))?(?:expired|closed)|applications (?:are )?closed)[.!]?$/i;
  for (const root of roots) {
    const primary = root.closest('[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]');
    const boundary = primary && [...primary.querySelectorAll('h2, [role="heading"]')].find(el =>
      el.checkVisibility({checkVisibilityCSS:true}) && /^(?:About the job|More jobs|Similar jobs|Recommended jobs)$/i.test(el.innerText.trim()));
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el.closest(excluded) || !el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) continue;
      if (boundary && (el.contains(boundary) || !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING))) continue;
      const text = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (closed.test(text)) return text;
    }
  }
  return '';
}"""

SNAPSHOT = r"""() => {
  const text = selectors => {
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (el.getClientRects().length && el.innerText?.trim()) return el.innerText.trim();
      }
    }
    return '';
  };
  const result = {
    title: text(['.job-details-jobs-unified-top-card__job-title h1', '.jobs-unified-top-card__job-title', '[data-test-job-title]', '.top-card-layout__title', 'main h1', 'h1']),
    company: text(['.job-details-jobs-unified-top-card__company-name', '.jobs-unified-top-card__company-name', '[data-test-company-name]', '.topcard__org-name-link', 'main a[href*="/company/"]']),
    location: text(['.job-details-jobs-unified-top-card__primary-description-container', '.jobs-unified-top-card__bullet', '[data-test-job-location]', '.topcard__flavor--bullet']),
    description: text(['#job-details', '.jobs-description__content', '[data-test-job-description]', '.show-more-less-html__markup']),
    workplace: '',
    employment_type: '',
    salary: '',
  };
  const typeLabel = /^(?:full[ -]time|part[ -]time|contract|temporary|permanent|fixed[ -]term|freelance|internship|apprenticeship|volunteer)(?:\s*[/,|]\s*(?:full[ -]time|part[ -]time|contract|temporary|permanent|fixed[ -]term))*$/i;
  const jobTypes = new Set();
  const pay = new Set();
  const excludedMetadata = '#job-details, .jobs-description, .jobs-description__content, [data-test-job-description], aside, .job-card-container, [componentkey^="job-card-component-ref-"], .jobs-similar-jobs, [role="dialog"]';
  for (const root of document.querySelectorAll('.job-details-jobs-unified-top-card, .jobs-unified-top-card, .jobs-details-top-card, .job-details-jobs-unified-top-card__job-insight, [data-test-employment-type], [data-test-salary], [data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]')) {
    const boundary = [...root.querySelectorAll('h2, [role="heading"]')].find(el => /^(?:About the job|More jobs|Similar jobs|Recommended jobs)$/i.test(el.innerText.trim()));
    for (const el of [root, ...root.querySelectorAll('span, p, li, button, div')]) {
      if (el.closest(excludedMetadata) || !el.checkVisibility({checkVisibilityCSS:true})) continue;
      if (boundary && (el.contains(boundary) || !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING))) continue;
      const label = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (typeLabel.test(label)) jobTypes.add(label);
      if (label.length < 160 && /(?:[£$€]|\b(?:GBP|USD|EUR)\b|\b(?:day|daily|hourly)[ -]rate\b)/i.test(label)) pay.add(label);
    }
  }
  result.employment_type = [...jobTypes].join(' / ');
  result.salary = [...pay].join('\n');
  const currentId = location.pathname.match(/\/jobs\/view\/(?:[^/]*-)?(\d+)/)?.[1];
  // SDUI descriptions have no legacy job-details selector. Stay within this
  // posting's About the job panel; other cards can describe unrelated roles.
  if (!result.description) {
    const excluded = 'aside, [componentkey^="job-card-component-ref-"], .job-card-container, .jobs-similar-jobs';
    for (const primary of document.querySelectorAll('[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.JobDetails"] section[aria-label="Primary content"]')) {
      const heading = [...primary.querySelectorAll('h2, [role="heading"]')].find(el =>
        !el.closest(excluded) && el.checkVisibility({checkVisibilityCSS:true}) && /^About the job$/i.test(el.innerText.trim()));
      if (!heading) continue;
      for (let panel = heading.parentElement; panel && panel !== primary; panel = panel.parentElement) {
        if (panel.closest(excluded) || panel.querySelectorAll('h2, [role="heading"][aria-level="2"]').length > 1) break;
        const copy = panel.cloneNode(true);
        copy.querySelectorAll(excluded + ', button, script, style').forEach(el => el.remove());
        copy.querySelectorAll('p, div, li, section, h2, h3, br').forEach(el => el.append(document.createTextNode('\n')));
        const description = (copy.textContent || '').trim();
        if (description && description !== heading.textContent.trim()) result.description = description;
      }
    }
  }
  // SDUI uses a paragraph for the job title. Its document title still identifies
  // the current posting as "Role | Company | LinkedIn". Do not use recommendations.
  if (currentId && !result.title) {
    const parts = document.title.split(/\s+\|\s+/);
    if (parts.length >= 3 && parts.at(-1).trim() === 'LinkedIn') {
      result.title = parts.slice(0, -2).join(' | ').replace(/^\(\d+\)\s*/, '').trim();
      result.company ||= parts.at(-2).trim();
    }
  }
  const postings = [];
  function visit(obj) {
    if (Array.isArray(obj)) return obj.forEach(visit);
    if (!obj || typeof obj !== 'object') return;
    if (obj['@type'] === 'JobPosting' || (Array.isArray(obj['@type']) && obj['@type'].includes('JobPosting'))) postings.push(obj);
    if (obj['@graph']) visit(obj['@graph']);
  }
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { visit(JSON.parse(script.textContent)); } catch {}
  }
  for (const p of postings) {
    const id = String(p.identifier?.value || '').match(/\d+$/)?.[0];
    const urlId = String(p.url || '').match(/\/jobs\/view\/(?:[^/]*-)?(\d+)/)?.[1];
    if ((id && id !== currentId) || (urlId && urlId !== currentId)) continue;
    if (!id && !urlId && postings.length !== 1) continue;
    result.title ||= p.title || '';
    result.company ||= p.hiringOrganization?.name || '';
    if (p.employmentType) result.employment_type += ' / ' + (Array.isArray(p.employmentType) ? p.employmentType.join(' / ') : p.employmentType);
    const salaryUnit = p.baseSalary?.value?.unitText;
    if (/^(?:DAY|HOUR)$/i.test(salaryUnit || '')) result.salary += '\n' + (/^DAY$/i.test(salaryUnit) ? 'day rate' : 'hourly rate');
    const places = Array.isArray(p.jobLocation) ? p.jobLocation : [p.jobLocation];
    result.location ||= places.map(x => {
      const a = x?.address || {};
      return [a.addressLocality, a.addressRegion, typeof a.addressCountry === 'string' ? a.addressCountry : a.addressCountry?.name].filter(Boolean).join(', ');
    }).filter(Boolean).join('; ');
    if (!result.description && p.description) {
      const doc = new DOMParser().parseFromString(p.description, 'text/html');
      doc.querySelectorAll('script,style').forEach(e => e.remove());
      result.description = doc.body.textContent.trim();
    }
    if (p.jobLocationType === 'TELECOMMUTE') result.workplace = 'Remote';
    break;
  }
  const workplace = text(['.job-details-jobs-unified-top-card__job-insight', '.jobs-unified-top-card__workplace-type', '[data-test-workplace-type]']);
  if (/\bremote\b/i.test(workplace)) result.workplace = 'Remote';
  else if (/\bhybrid\b/i.test(workplace)) result.workplace = 'Hybrid';
  else if (/\bon.site\b/i.test(workplace)) result.workplace = 'On-site';
  return result;
}"""


# Share names across required-field detection, selection and review summaries.
# Employer headings can be localized even when LinkedIn's controls are English.
RESUME_NAMES = r'(?:resume|résumé|cv|lebenslauf|curr[ií]cul(?:um|o)(?:\s+vitae)?)'
RESUME_DOM = r"""
  const resume = /(?:^|[\s_\-])__RESUME_NAMES__(?=$|[\s_*:\-])/i;
  const resumeLabel = /^(?:(?:upload|select|choose|your)\s+)?__RESUME_NAMES__(?:\s*\/\s*__RESUME_NAMES__)?\s*\*?$/i;
  const cover = /\b(?:cover\s*letter|anschreiben|portfolio)\b/i;
  const headingSelector = 'h1,h2,h3,h4,p,legend,label,[role="heading"]';
  const fold = text => text.normalize('NFKC').replace(/\s+/g, ' ').trim();
  // A localized employer heading can sit above LinkedIn's standard English
  // upload instructions. Keep this evidence local to the document picker,
  // never borrowing a filename or heading from another question.
  const pickerContext = (control, root) => {
    for (let node = control.parentElement; node; node = node.parentElement) {
      const groups = [...node.querySelectorAll('fieldset, [role="radiogroup"]')]
        .filter(e => !e.parentElement?.closest('fieldset, [role="radiogroup"]'));
      if (groups.length > 1) break;
      const labels = [...node.querySelectorAll(headingSelector)]
        .filter(e => e.checkVisibility()).map(e => fold(e.innerText || ''));
      const ownLabel = fold((node.getAttribute('aria-label') || '') + ' ' +
        (node.getAttribute('aria-labelledby') || '').split(/\s+/)
          .map(id => document.getElementById(id)?.textContent || '').join(' '));
      const legend = node.matches('fieldset, [role="radiogroup"]') ? node.querySelector('legend')?.innerText || ownLabel : '';
      if (cover.test(legend) || labels.some(t => /^(?:cover\s*letter|anschreiben|portfolio)\s*\*?$/i.test(t))) return {resume:false, required:false};
      const help = labels.some(t => /^select or upload a resume in DOC, DOCX, or PDF format\b/i.test(t));
      const named = [ownLabel, ...labels].filter(t => resumeLabel.test(t));
      if (named.length || help) {
        return {resume:true, required: control.required || control.getAttribute('aria-required') === 'true' ||
          control.closest('fieldset, [role="radiogroup"]')?.getAttribute('aria-required') === 'true' ||
          (help ? [ownLabel, ...labels] : named).some(t => t.endsWith('*'))};
      }
      // An explicit unrelated question is a boundary, even beside a CV picker.
      if (legend.trim()) break;
      if (node === root) break;
    }
    return {resume:false, required:false};
  };
""".replace('__RESUME_NAMES__', RESUME_NAMES)

RESUME_REQUIRED = r"""root => {""" + RESUME_DOM + r"""
  const headings = [...root.querySelectorAll('h1,h2,h3,h4,p,legend,label,[role="heading"]')];
  if (headings.some(e => e.checkVisibility() && resumeLabel.test(e.innerText.trim()) && e.innerText.includes('*'))) return true;
  for (const e of root.querySelectorAll('input[type="file"], input[type="radio"], [role="radiogroup"]')) {
    const context = pickerContext(e, root);
    if (context.resume && context.required) return true;
    if (!e.required && e.getAttribute('aria-required') !== 'true') continue;
    const label = [...(e.labels || [])].map(x => x.innerText).join(' ') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.id || '') + ' ' + (e.name || '');
    if (resume.test(label) || e.closest('[data-test-resume-card], .jobs-resume-picker__resume, .jobs-document-upload-redesign-card__container')) return true;
    if (e.type === 'radio' && headings.some(h => h.checkVisibility() && resumeLabel.test(h.innerText.trim()))) {
      const text = e.closest('[role="radio"]')?.getAttribute('aria-label') || e.parentElement?.innerText || '';
      if (/\.(?:pdf|docx?)\b/i.test(text)) return true;
    }
  }
  return false;
}"""

SEMANTIC_RESUMES = r"""root => {""" + RESUME_DOM + r"""
  const selector = 'input[type="radio"], [role="radio"]';
  const file = /\.(?:pdf|docx?)(?:\b|$)/i;
  const sectionLabel = text => resumeLabel.test(text) || /^(?:cover\s*letter|anschreiben)\s*\*?$/i.test(text);
  // A native input inside role=radio is one option, not two options.
  const choices = node => [...node.querySelectorAll(selector)].filter(e =>
    !e.matches('[role="radio"]') || !e.querySelector('input[type="radio"]'));
  const refs = e => (e.getAttribute('aria-labelledby') || '').split(/\s+/)
    .map(id => document.getElementById(id)?.textContent || '').join(' ');
  const own = e => [...(e.labels || [])].map(x => x.innerText).join(' ') + ' ' +
    (e.getAttribute('aria-label') || '') + ' ' + refs(e) + ' ' + (e.innerText || '');
  // Old annotations must not survive a form rerender or a later application step.
  root.querySelectorAll('[data-ea-resume-choice], [data-ea-resume-target]').forEach(e => {
    e.removeAttribute('data-ea-resume-choice'); e.removeAttribute('data-ea-resume-target');
  });
  let count = 0;
  for (const radio of choices(root)) {
    let text = own(radio), target = [...(radio.labels || [])].find(e => e.checkVisibility()) || radio;
    const wrapper = radio.closest('[role="radio"]');
    if (wrapper && wrapper !== radio) {
      text += ' ' + own(wrapper);
      target = wrapper;
    }
    // Find the filename beside this control, without borrowing one from another
    // option or an unrelated question elsewhere in the modal.
    for (let node = radio.parentElement; node && node !== root; node = node.parentElement) {
      if (choices(node).length !== 1) break;
      if (file.test(node.innerText || '')) {
        text += ' ' + node.innerText;
        // Clicking the filename can preview/download it. Prefer the radio.
        if (!wrapper) target = node;
        break;
      }
    }
    const shown = radio.checkVisibility({checkVisibilityCSS:true}) ||
      [...(radio.labels || [])].some(e => e.checkVisibility({checkVisibilityCSS:true})) ||
      (target !== radio && target.checkVisibility({checkVisibilityCSS:true}));
    if (!shown) continue;
    const picker = pickerContext(radio, root);
    let context = '';
    for (let node = radio.parentElement; node; node = node.parentElement) {
      const heading = node.querySelector('legend, h1, h2, h3, h4, [role="heading"]');
      const paragraph = [...node.querySelectorAll('p, label')].find(e => sectionLabel(e.textContent.trim()));
      const label = (node.getAttribute('aria-label') || '') + ' ' + refs(node) + ' ' + (heading?.textContent || '') + ' ' + (paragraph?.textContent || '');
      if (resume.test(label) || cover.test(label)) { context = label; break; }
      if (node === root) break;
    }
    if (cover.test(text) || cover.test(context)) continue;
    const identity = (radio.name || '') + ' ' + (radio.id || '');
    const resumeContext = picker.resume || resume.test(text) || resume.test(context) || /resume|résumé|lebenslauf|(?:^|[-_])cv(?:$|[-_])/i.test(identity);
    if (!(file.test(text) && resumeContext) && !(/\b(?:select|choose)\s+/i.test(text) && resume.test(text))) continue;
    radio.setAttribute('data-ea-resume-choice', 'true');
    target.setAttribute('data-ea-resume-target', String(count++));
  }
  return count;
}"""


def first_uploaded_resume(dialog, check=lambda: None):
    """Select the first rendered resume card and verify selection.

    Returns (resume_step, selected, detail). Cover letters are not selected.
    """
    # The final review shows a read-only copy of the selected document. A Resume
    # heading there does not mean the user must choose it again.
    summary = dialog.evaluate(r'''root => {''' + RESUME_DOM + r'''
      const nodes = [...root.querySelectorAll('h1,h2,h3,h4,p,legend,[role="heading"]')];
      if (!nodes.some(e => e.checkVisibility() && /^review (?:your )?application$/i.test(e.innerText.trim()))) return false;
      for (const heading of nodes.filter(e => e.checkVisibility() && resumeLabel.test(e.innerText.trim()))) {
        for (let section = heading.parentElement; section && section !== root; section = section.parentElement) {
          if (section.querySelector('input[type="radio"], [role="radio"], input[type="file"]')) break;
          if (/\.(?:pdf|docx?)\b/i.test(section.innerText)) return true;
        }
      }
      return false;
    }''')
    if summary:
        return True, True, 'Selected resume displayed in the application review'
    cards = dialog.locator('[data-test-resume-card], .jobs-document-upload-redesign-card__container, .jobs-resume-picker__resume')
    candidates = []
    for card in cards.all():
        if not card.is_visible():
            continue
        context = card.evaluate("e => (e.closest('fieldset, .jobs-easy-apply-form-section__grouping')?.innerText || '').slice(0,500)")
        if 'cover letter' in context.lower() and not any(w in context.lower() for w in ['resume', 'résumé']):
            continue
        candidates.append(card)
    if not candidates:
        # Filename siblings, aria-labelledby and unnamed/custom radio controls
        # are common on CV-only pages. Recognize every resume option before
        # generic question handling; never blanket-ignore unknown radios.
        check()
        if dialog.evaluate(SEMANTIC_RESUMES):
            radio = dialog.locator('[data-ea-resume-choice]').first
            selected = lambda: radio.evaluate("e => e.matches('input:checked, [aria-checked=\"true\"]')")
            if not selected():
                if radio.get_attribute('type') == 'radio' and radio.is_visible():
                    radio.check()
                else:
                    dialog.locator('[data-ea-resume-target="0"]').click()
            return True, bool(selected()), "First uploaded CV selected" if selected() else "Could not verify selection of the first uploaded CV"
        is_resume_step = dialog.evaluate(r'''root => {''' + RESUME_DOM + r'''
          return [...root.querySelectorAll('input[type="file"]')].some(e =>
            resume.test(e.id + ' ' + [...(e.labels || [])].map(x => x.innerText).join(' '))) ||
            [...root.querySelectorAll('h1,h2,h3,h4,p,legend,[role="heading"]')].some(e =>
              e.checkVisibility() && resumeLabel.test(e.innerText.trim()));
        }''')
        return is_resume_step, False, "Could not identify your first uploaded resume card"
    for card in candidates:
        card.evaluate("e => e.setAttribute('data-ea-resume-card','true')")
    first = candidates[0]
    selected_js = """e => !!(e.matches('[aria-checked="true"], [aria-selected="true"], [data-selected="true"]') ||
      e.querySelector('input:checked, [aria-checked="true"], [aria-selected="true"]') ||
      [...e.classList].some(c => /(?:^|--|__)selected$/.test(c)))"""
    check()
    if not first.evaluate(selected_js):
        choices = first.locator('input[type="radio"], [role="radio"], button[aria-label*="select" i], button[aria-label*="choose" i], .jobs-document-upload-redesign-card__toggle-button')
        if not choices.count():
            return True, False, "First resume card found but its selection control is not recognized"
        choice = choices.first
        if choice.get_attribute('type') == 'radio':
            choice.check()
        else:
            choice.click()
    return True, bool(first.evaluate(selected_js)), first.inner_text().strip()[:200]

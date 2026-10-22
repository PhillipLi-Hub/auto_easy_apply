"""Exclude non-permanent engagements without matching contract-related skills."""

import re


PREFIX = 'Excluded: full-time permanent roles only'
TYPES = r'(?:contract(?:or)?|temporary|temp|fixed[ -]term|ftc|freelance|part[ -]time|interim|internship|intern|apprenticeship|volunteer|zero[ -]hours?)'
RATE = r'(?:\b(?:day|daily|hourly)[ -]rate\b|\b(?:inside|outside)\s+ir35\b|(?:[£$€]\s*\d[\d,.]*(?:\s*[-–]\s*[£$€]?\s*\d[\d,.]*)?|\d[\d,.]*\s*(?:gbp|usd|eur))\s*(?:/|per\s+|a\s+)(?:day|hour|hr)\b|\b\d[\d,.]*\s*(?:gbp\s*)?p[dh]\b)'


def clean(value):
    if isinstance(value, list):
        value = ' / '.join(str(item) for item in value)
    return str(value or '').casefold().replace('_', ' ').replace('–', '-').replace('‑', '-')


def employment_rejection(job):
    title = clean(job.get('title'))
    employment = clean(job.get('employment_type'))
    description = clean(job.get('description'))
    salary = clean(job.get('salary'))
    # Structured fields and short job-type labels describe the engagement.
    if re.search(rf'\b{TYPES}\b', employment):
        return PREFIX + ' (employment type: ' + employment + ')'
    # "Smart contract engineer" describes a skill, not a contract engagement.
    role_title = re.sub(r'\b(?:smart|futures?|options?|derivatives?|commercial) contracts?\b|\bcontract (?:management|negotiation|law)\b', '', title)
    if re.search(rf'\b{TYPES}\b', role_title):
        return PREFIX + ' (non-permanent or part-time title)'
    for text in (title, salary, description):
        if re.search(RATE, text):
            return PREFIX + ' (day/hour rate or IR35 engagement)'
    for line in re.split(r'[\n;.!]+', description):
        line = line.strip()
        # Do not exclude an ordinary permanent employment contract, contract
        # negotiation experience, or a description of other teams' contractors.
        if re.search(r'\b(?:not a contract|no contract work|not temporary)\b', line):
            continue
        if re.fullmatch(rf'{TYPES}(?:\s*[-:/,|]\s*(?:full[ -]time|\d+ months?))?', line):
            return PREFIX + ' (engagement stated in description)'
        patterns = [
            r'^contract(?: length| duration)?\s*[:(=-]\s*\d+\s*[- ]?\s*(?:months?|weeks?|years?)\b',
            rf'\b(?:job type|employment type|employment|engagement|contract type|position type|type|duration)\s*[:=-]\s*(?:full[ -]time\s*[/,|-]\s*)?{TYPES}\b',
            rf'\b(?:\d+|three|six|twelve|eighteen)\s*[- ]?\s*(?:months?|weeks?|years?)\s+(?:initial\s+)?(?:contract|ftc|fixed[ -]term)\b',
            rf'\b{TYPES}\s+(?:role|position|opportunity|basis|assignment|engagement|contract)\b',
            rf'\b(?:this|the)\s+(?:role|position|opportunity)\s+(?:is|will be)\s+(?:a |an |on a )?{TYPES}\b',
            r'\b(?:contract[- ]to[- ]hire|temp[- ]to[- ]perm|maternity cover)\b',
        ]
        if any(re.search(pattern, line) for pattern in patterns):
            return PREFIX + ' (engagement stated in description)'
    return None

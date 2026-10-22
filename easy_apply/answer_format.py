"""Fit known answers to controls without truncating facts or converting money."""

import re


def format_answer(answer, info):
    limit = info.get('maxlength')
    numeric = info['type'] == 'number' or info.get('inputmode') in {'numeric', 'decimal'}
    if re.search(r'\bsalary\b', info['label'], re.I):
        # Only compact an unambiguous saved amount. Ranges, other currencies,
        # hourly rates and free-form explanations must not be guessed.
        match = re.fullmatch(r'\s*(?:(at least)\s+)?(?:GBP|£)\s*([\d,]+)(?:\s+(?:per year|per annum))?\.?\s*', answer, re.I)
        if match and not re.search(r'\b(?:EUR|euros?|USD|dollars?|hour|day|month|weekly|net)\b|[€$]', info['label'], re.I):
            amount = match[2].replace(',', '')
            if numeric or (limit and len(answer) > limit):
                # Salary controls often enforce numbers server-side even
                # though their HTML type is text. Keep the stated amount.
                answer = amount
    if limit and len(answer) > limit:
        raise ValueError(f"answer exceeds the {limit}-character limit; save a shorter answer")
    if numeric and not re.fullmatch(r'-?\d+(?:\.\d+)?', answer):
        raise ValueError('this field requires a numeric answer')
    return answer

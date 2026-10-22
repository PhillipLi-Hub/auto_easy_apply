"""Title-only exclusions for the user's preferred role level."""
import re


EXCLUDED_LEVEL = re.compile(
    r'\b(?:senior|sr|director|manager|mgr|principal|principle|staff|'
    r'head(?![\s-]+office\b)|chief|president|[aes]?vp|ceo|cto|cfo|coo|cdo|caio|'
    r'lead(?![\s-]+(?:generation|qualification|generating)\b)|team[\s-]+leader)\b',
    re.IGNORECASE,
)


def seniority_rejection(job):
    match = EXCLUDED_LEVEL.search(job.get('title', '') or '')
    if match:
        return f'Excluded role level in title: {match.group(0)} (senior or leadership role)'
    return None

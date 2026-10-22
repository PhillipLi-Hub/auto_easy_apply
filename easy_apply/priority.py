"""Rank quant roles and listing freshness without treating retries as new jobs."""

import re
import json
from datetime import datetime, timedelta, timezone


def timestamp(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
    except (ValueError, TypeError, AttributeError):
        return None


def listing_date(value, now):
    date = timestamp(value)
    if date:
        return date.isoformat() if date <= now else ''
    text = value.strip().casefold()
    if text in {'just now', 'today'}:
        return now.isoformat()
    match = re.fullmatch(r'(?:reposted\s+)?(\d+)\+?\s+(minute|hour|day|week|month|year)s?\s+ago', text)
    if not match:
        return ''
    days = {'minute': 1 / 1440, 'hour': 1 / 24, 'day': 1, 'week': 7, 'month': 30, 'year': 365}
    return (now - timedelta(days=int(match[1]) * days[match[2]])).isoformat()


def queue_order(row, settings, now=None):
    now = now or datetime.now(timezone.utc)
    title = row.get('title', '').strip()
    quant = bool(re.search(r'\bquant(?:itative)?\b', title, re.I))
    category = (0 if quant else 1 if title else 2) if settings.prioritize_quant else (0 if title else 1)
    posted = timestamp(row.get('posted_at'))
    recent_evidence = None
    if not posted:
        try:
            snapshot = json.loads(row.get('snapshot') or '{}')
            recent_evidence = timestamp(snapshot.get('search_listing', {}).get('recent_search_at'))
        except (ValueError, TypeError, AttributeError):
            pass
    # Discovery alone cannot make an old job "posted in the last 24 hours".
    # If the posting date is absent, retain recent-search evidence for one day.
    recent_date = posted or recent_evidence
    recent = bool(recent_date and timedelta(0) <= now - recent_date <= timedelta(hours=24))
    group = (0 if recent else 1) if settings.newest_first else 0
    date = posted or timestamp(row.get('first_seen'))
    freshness = -(date.timestamp() if date else 0) if settings.newest_first else 0
    return group, category, freshness, -row.get('retry_priority', 0)

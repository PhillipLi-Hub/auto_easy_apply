"""Cumulative daily reports from the immutable confirmation ledger."""

import csv
import html
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


LONDON = ZoneInfo('Europe/London')
CATEGORIES = ('Quant', 'Data Scientist', 'Software Engineer', 'Trader',
              'Machine Learning Engineer', 'AI Engineer', 'Risk', 'Other')


def local_day(stamp=None):
    if stamp is None:
        return datetime.now(LONDON).date().isoformat()
    instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(LONDON).date().isoformat()


def role_category(title):
    title = title.casefold()
    rules = (
        ('Quant', r'\bquant(?:itative)?\b'),
        ('Trader', r'\btrad(?:er|ing)\b'),
        ('Risk', r'\brisk\b'),
        ('Machine Learning Engineer', r'\b(?:machine[ -]learning|ml|mlops|deep learning)\b'),
        ('Data Scientist', r'\bdata scien(?:tist|ce)\b'),
        ('AI Engineer', r'\b(?:ai|artificial intelligence|llm|genai|generative ai|agentic ai)\b'),
        ('Software Engineer', r'\b(?:software|developer|programmer|backend|back-end|frontend|front-end|full[ -]stack|devops|sre)\b'),
    )
    return next((name for name, pattern in rules if re.search(pattern, title)), 'Other')


def begin_run(store, action, scheduled_day=None):
    with store.connect() as db:
        return db.execute("INSERT INTO application_runs(started_at,action,scheduled_day,outcome) VALUES(?,?,?,'running')",
                          (store.now(), action, scheduled_day)).lastrowid


def finish_run(store, identifier, outcome, detail):
    with store.connect() as db:
        db.execute('UPDATE application_runs SET finished_at=?,outcome=?,detail=? WHERE id=?',
                   (store.now(), outcome, detail, identifier))
        start, end = db.execute('SELECT started_at,finished_at FROM application_runs WHERE id=?', (identifier,)).fetchone()
    first, last = date.fromisoformat(local_day(start)), date.fromisoformat(local_day(end))
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def report_days(store):
    with store.connect() as db:
        stamps = [row[0] for row in db.execute('SELECT submitted_at FROM confirmed_applications UNION SELECT started_at FROM application_runs UNION SELECT finished_at FROM application_runs WHERE finished_at IS NOT NULL')]
    return sorted({local_day()} | {local_day(stamp) for stamp in stamps}, reverse=True)


def application_totals(store):
    """Use the same confirmation ledger and London dates as daily reports."""
    today = local_day()
    with store.connect() as db:
        stamps = [row[0] for row in db.execute('SELECT submitted_at FROM confirmed_applications')]
    return sum(local_day(stamp) == today for stamp in stamps), len(stamps)


def build_report(store, day):
    day = date.fromisoformat(day).isoformat()
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        applications = [dict(row) for row in db.execute('SELECT * FROM confirmed_applications ORDER BY submitted_at,job_id') if local_day(row['submitted_at']) == day]
        runs = [dict(row) for row in db.execute('SELECT * FROM application_runs ORDER BY started_at,id')
                if local_day(row['started_at']) <= day <= local_day(row['finished_at'] or store.now())]
    counts = dict.fromkeys(CATEGORIES, 0)
    for row in applications:
        row['category'] = role_category(row['title'])
        row['local_time'] = datetime.fromisoformat(row['submitted_at'].replace('Z', '+00:00')).astimezone(LONDON).isoformat(timespec='seconds')
        counts[row['category']] += 1
    return {'date': day, 'timezone': 'Europe/London', 'generated_at': datetime.now(LONDON).isoformat(timespec='seconds'),
            'total_confirmed': len(applications), 'counts': counts, 'applications': applications, 'runs': runs,
            'approximate_date_count': sum(row['timestamp_basis'] == 'legacy_updated' for row in applications),
            'unverified_current': sum(row['status'] in {'unverified', 'submitting'} for row in store.rows()),
            'counting_rule': 'One category per confirmed job. Priority for overlapping titles: Quant, Trader, Risk, ML, Data Scientist, AI, Software Engineer, Other. Already-applied discoveries, manual marks and unverified submissions are excluded.'}


def summary(report):
    lines = [f"{report['date']} · Europe/London", f"Confirmed applications: {report['total_confirmed']}", '']
    lines.extend(f'{category}: {report["counts"][category]}' for category in CATEGORIES)
    lines.extend(['', f"Awaiting verification across the queue (not counted): {report['unverified_current']}"])
    if report['approximate_date_count']:
        lines.append(f"Historical dates are approximate for {report['approximate_date_count']} applications (old record update time).")
    if report['runs']:
        lines.extend(['', 'Runs:'] + [f"{run['action']}: {run['outcome']} — {run['detail']}" for run in report['runs']])
    return '\n'.join(lines)


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    counts = ''.join(f'<tr><td>{esc(key)}</td><td>{value}</td></tr>' for key, value in report['counts'].items())
    applications = []
    for row in report['applications']:
        # Canonical job links only; employer-controlled text is always escaped.
        url = f"https://www.linkedin.com/jobs/view/{row['job_id']}/" if re.fullmatch(r'\d+', row['job_id']) else '#'
        basis = 'Approximate historical date' if row['timestamp_basis'] == 'legacy_updated' else row['timestamp_basis'].replace('_', ' ')
        applications.append(f'<tr><td>{esc(row["category"])}</td><td><a href="{esc(url)}">{esc(row["title"] or row["job_id"])}</a></td><td>{esc(row["company"])}</td><td>{esc(row["local_time"])}</td><td>{esc(basis)}</td></tr>')
    jobs = ''.join(applications) or '<tr><td colspan="5">No confirmed applications recorded for this day.</td></tr>'
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Applications — {esc(report['date'])}</title><style>
body{{font:16px system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 24px;color:#172438;background:#f5f7fa}}
table{{width:100%;border-collapse:collapse;background:white;margin:20px 0}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dde3ec}}th{{background:#e6eef8}}a{{color:#0759a8}}pre{{white-space:pre-wrap;font:inherit}}.total{{font-size:32px;color:#07685c}}
</style><h1>Daily application report · {esc(report['date'])}</h1><p>Europe/London · Generated {esc(report['generated_at'])}</p>
<p class="total">{report['total_confirmed']} confirmed applications</p>
<table><thead><tr><th>Role category</th><th>Applications</th></tr></thead><tbody>{counts}</tbody></table>
<pre>{esc(summary(report))}</pre><p>{esc(report['counting_rule'])}</p>
<h2>Confirmed roles</h2><table><thead><tr><th>Category</th><th>Role</th><th>Company</th><th>Time (London)</th><th>Date basis</th></tr></thead><tbody>{jobs}</tbody></table></html>'''


def save_report(store, day):
    report = build_report(store, day)
    directory = store.path.parent / 'reports' / 'daily'
    directory.mkdir(parents=True, exist_ok=True)
    for suffix, text in (('.json', json.dumps(report, indent=2, ensure_ascii=False)), ('.html', render_html(report))):
        path = directory / (report['date'] + suffix)
        temporary = path.with_suffix(suffix + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    path = directory / (report['date'] + '.csv')
    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Category', 'Confirmed applications'])
        writer.writerows(report['counts'].items())
        writer.writerow(['Total', report['total_confirmed']])
    temporary.replace(path)
    return report, directory / (report['date'] + '.html')

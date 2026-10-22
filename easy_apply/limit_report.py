"""Local report for a platform daily limit; separate from the app's run cap."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo


def daily_limit_report(store, message, confirmed_in_run=None, job_id=None):
    rows = store.rows()
    return {
        'event': 'linkedin_daily_limit',
        'detected_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'linkedin_message': message,
        'confirmed_in_run': confirmed_in_run,
        'queued_remaining': sum(row['status'] == 'queued' for row in rows),
        'unverified_job_ids': [row['id'] for row in rows if row['status'] in {'submitting', 'unverified'}],
        'current_job_id': job_id,
    }


def save_limit_report(store, report):
    directory = store.path.parent / 'reports'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"daily-limit-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return path


def limit_status(report):
    return f"Today's complete — LinkedIn daily limit reached. {report['queued_remaining']} jobs remain queued for another day."


def today_limit_report(store):
    """Restore a confirmed daily-limit notice, but never yesterday's status."""
    london = ZoneInfo('Europe/London')
    today = datetime.now(london).date()
    directory = store.path.parent / 'reports'
    for path in sorted(directory.glob('daily-limit-*.json'), reverse=True):
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
            if (report.get('event') == 'linkedin_daily_limit'
                    and datetime.fromisoformat(report['detected_at']).astimezone(london).date() == today):
                report['queued_remaining'] = len(store.rows('queued'))
                report['report_path'] = str(path)
                return report
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def limit_details(report):
    lines = [limit_status(report), '', f"Detected: {report['detected_at']}"]
    if report['confirmed_in_run'] is not None:
        lines.append(f"Confirmed applications this run: {report['confirmed_in_run']}")
    if report['unverified_job_ids']:
        lines.append(f"Awaiting submission verification: {len(report['unverified_job_ids'])}. Check LinkedIn before retrying these jobs.")
    lines.extend(['', 'LinkedIn message: ' + report['linkedin_message']])
    if report.get('report_path'):
        lines.extend(['', 'Report saved: ' + report['report_path']])
    if report.get('save_error'):
        lines.extend(['', 'Could not save report: ' + report['save_error']])
    return '\n'.join(lines)

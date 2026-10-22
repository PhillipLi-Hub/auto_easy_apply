import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import DATA, job_id, normalize
from .questions import questions_from_note, answered, QUESTION_NOTE_PREFIX
from .priority import listing_date
from .clearance import requires_sc, REASON as SC_REJECTION
from .employment import employment_rejection
from .seniority import seniority_rejection


def status_label(row):
    if row['status'] != 'applied':
        return row['status']
    return {'app_confirmed': 'submitted by app', 'linkedin_history': 'already applied',
            'manual_verified': 'manually verified'}.get(row.get('application_source'), 'applied (source unknown)')


def queue_summary(rows, settings=None):
    def count(status, source=None):
        return sum(r['status'] == status and (source is None or r.get('application_source', '') == source) for r in rows)
    parts = [f"Database: {len(rows)} jobs", f"Queued (all searches): {count('queued')}",
             f"Submitted by app: {count('applied', 'app_confirmed')}",
             f"Already applied on LinkedIn: {count('applied', 'linkedin_history')}"]
    if settings is not None:
        terms = [normalize(t) for t in settings.title_include.split(',') if t.strip()]
        matching = sum(r['status'] == 'queued' and (not terms or any(t in normalize(r['title']) for t in terms)) for r in rows)
        parts.insert(2, f'Queued title matches: {matching}')
    for label, total in [('Manually verified', count('applied', 'manual_verified')),
                         ('Applied, source unknown', count('applied', '')),
                         ('Unverified', count('unverified')), ('Need attention', count('needs_input'))]:
        if total:
            parts.append(f'{label}: {total}')
    return ' · '.join(parts)


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DATA / "applications.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
                note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
                snapshot TEXT NOT NULL DEFAULT '{}')""")
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'application_source' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN application_source TEXT NOT NULL DEFAULT ''")
            if 'retry_priority' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN retry_priority INTEGER NOT NULL DEFAULT 0")
            for name in ('first_seen', 'posted_at', 'submission_attempted_at'):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # Upgrade existing history only when the old note records its origin.
            db.execute("""UPDATE jobs SET application_source=CASE
                WHEN note IN ('LinkedIn displayed an application-sent confirmation',
                              'LinkedIn confirmed the submitted application after cooldown') THEN 'app_confirmed'
                WHEN note LIKE 'Search result shows already applied:%'
                  OR note LIKE 'Already applied on LinkedIn:%' THEN 'linkedin_history'
                WHEN note='User marked applied after manual verification' THEN 'manual_verified'
                ELSE '' END WHERE status='applied' AND application_source=''""")
            db.execute("""CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT,
                status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""CREATE TABLE IF NOT EXISTS question_occurrences (
                job_id TEXT NOT NULL, question_key TEXT NOT NULL, spec TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id, question_key))""")
            db.execute('''CREATE TABLE IF NOT EXISTS confirmed_applications (
                job_id TEXT PRIMARY KEY, submitted_at TEXT NOT NULL, confirmed_at TEXT NOT NULL,
                timestamp_basis TEXT NOT NULL, title TEXT NOT NULL, company TEXT NOT NULL, url TEXT NOT NULL)''')
            # Preserve old records without pretending their last update was an
            # exact submission time. New confirmations have a dedicated ledger.
            db.execute('''INSERT OR IGNORE INTO confirmed_applications
                SELECT id,updated,updated,'legacy_updated',title,company,url FROM jobs
                WHERE status='applied' AND application_source='app_confirmed' ''')
            db.execute('''CREATE TABLE IF NOT EXISTS application_runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                action TEXT NOT NULL, scheduled_day TEXT, outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')''')

    def import_question_notes(self):
        """Backfill old blocked jobs without reviving questions already resolved."""
        with self.connect() as db:
            for identifier, note in db.execute("SELECT id,note FROM jobs WHERE status='needs_input'").fetchall():
                for spec in questions_from_note(note):
                    db.execute('INSERT OR IGNORE INTO question_occurrences(job_id,question_key,spec) VALUES(?,?,?)',
                               (identifier, normalize(spec['label']), json.dumps(spec)))

    def record_questions(self, identifier, specs):
        with self.connect() as db:
            db.execute('UPDATE question_occurrences SET resolved=1 WHERE job_id=?', (identifier,))
            for spec in specs:
                if not spec.get('label') or spec['label'].lower().startswith('unlabelled'):
                    continue
                db.execute('''INSERT INTO question_occurrences(job_id,question_key,spec) VALUES(?,?,?)
                              ON CONFLICT(job_id,question_key) DO UPDATE SET spec=excluded.spec,resolved=0''',
                           (identifier, normalize(spec['label']), json.dumps(spec)))

    def unanswered_questions(self, settings):
        with self.connect() as db:
            records = db.execute('''SELECT q.question_key,q.spec,j.id,j.title,j.company
                FROM question_occurrences q JOIN jobs j ON j.id=q.job_id
                WHERE q.resolved=0 AND j.status='needs_input' ''').fetchall()
        groups = {}
        for key, raw, identifier, title, company in records:
            spec = json.loads(raw)
            if spec.get('source') == 'legacy' and not questions_from_note(QUESTION_NOTE_PREFIX + spec['label']):
                continue
            if answered(spec, settings):
                continue
            group = groups.setdefault(key, {'key': key, 'label': spec['label'], 'specs': [], 'jobs': []})
            group['specs'].append(spec)
            group['jobs'].append({'id': identifier, 'title': title, 'company': company})
        for group in groups.values():
            group['count'] = len(group['jobs'])
        return sorted(groups.values(), key=lambda group: (-group['count'], group['key']))

    def resolve_question(self, key, settings):
        """Resolve the saved question and requeue only jobs with no other unanswered questions."""
        requeued = 0
        with self.connect() as db:
            identifiers = [row[0] for row in db.execute(
                'SELECT job_id FROM question_occurrences WHERE question_key=? AND resolved=0', (key,))]
            db.execute('UPDATE question_occurrences SET resolved=1 WHERE question_key=?', (key,))
            for identifier in identifiers:
                remaining = db.execute('SELECT spec FROM question_occurrences WHERE job_id=? AND resolved=0', (identifier,)).fetchall()
                if any(not answered(json.loads(raw), settings) for (raw,) in remaining):
                    continue
                cursor = db.execute('''UPDATE jobs SET status='queued',note='',retry_priority=1,updated=?
                    WHERE id=? AND status='needs_input' AND note LIKE ?''',
                    (self.now(), identifier, QUESTION_NOTE_PREFIX + '%'))
                requeued += cursor.rowcount
        return requeued

    def start_scan(self, details):
        with self.connect() as db:
            return db.execute("INSERT INTO scans(started,status,details) VALUES(?,?,?)",
                              (self.now(), 'running', json.dumps(details))).lastrowid

    def finish_scan(self, identifier, status, details):
        with self.connect() as db:
            db.execute("UPDATE scans SET finished=?,status=?,details=? WHERE id=?",
                       (self.now(), status, json.dumps(details), identifier))

    def save_listing_metadata(self, identifier, listing):
        """Enrich a saved listing without resetting application state or notes."""
        with self.connect() as db:
            row = db.execute("SELECT title,company,snapshot FROM jobs WHERE id=?", (identifier,)).fetchone()
            if not row:
                return
            snapshot = json.loads(row[2])
            previous = snapshot.get('search_listing', {})
            posting = listing.get('posted_datetime') or listing.get('posted_text') or ''
            previous_posting = previous.get('posted_datetime') or previous.get('posted_text') or ''
            if posting and posting != previous_posting:
                date = listing_date(posting, datetime.now(timezone.utc))
                if date:
                    db.execute('UPDATE jobs SET posted_at=? WHERE id=?', (date, identifier))
            snapshot['search_listing'] = previous | {key: value for key, value in listing.items() if value or key not in previous}
            db.execute("UPDATE jobs SET title=?,company=?,snapshot=? WHERE id=?",
                       (listing.get('title') or row[0], listing.get('company') or row[1], json.dumps(snapshot), identifier))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, url):
        identifier = job_id(url)
        canonical = f"https://www.linkedin.com/jobs/view/{identifier}/"
        with self.connect() as db:
            now = self.now()
            result = db.execute("INSERT OR IGNORE INTO jobs(id,url,updated,first_seen) VALUES(?,?,?,?)", (identifier, canonical, now, now))
        return bool(result.rowcount)

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def rows(self, status=None):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            query = "SELECT * FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY CASE WHEN status='queued' THEN retry_priority ELSE 0 END DESC,updated,id"
            return [dict(row) for row in db.execute(query, (status,) if status else ())]

    def exclude_clearance_jobs(self, settings):
        """Remove known SC-required jobs from pending work; retain history."""
        if not settings.exclude_sc_required:
            return 0
        return self._exclude_pending(lambda job: SC_REJECTION if requires_sc(job.get('title', ''), job.get('description', '')) else None)

    def exclude_ineligible_jobs(self, settings):
        count = self.exclude_clearance_jobs(settings)
        if settings.exclude_senior_roles:
            count += self._exclude_pending(seniority_rejection)
        if settings.permanent_full_time_only:
            count += self._exclude_pending(employment_rejection)
        return count

    def _exclude_pending(self, rejection):
        count = 0
        with self.connect() as db:
            rows = db.execute("SELECT id,title,snapshot FROM jobs WHERE status IN ('queued','needs_input','error')").fetchall()
            for identifier, title, raw in rows:
                snapshot = json.loads(raw)
                listing = snapshot.get('search_listing', {})
                job = listing | {key: value for key, value in snapshot.items() if value}
                job['title'] = title or job.get('title', '')
                reason = rejection(job)
                if reason:
                    count += db.execute("UPDATE jobs SET status='skipped',note=?,updated=? WHERE id=? AND status IN ('queued','needs_input','error')",
                                        (reason, self.now(), identifier)).rowcount
        return count

    def update(self, identifier, status, note="", job=None, source=None):
        if source is not None and source not in {'', 'app_confirmed', 'linkedin_history', 'manual_verified'}:
            raise ValueError('Unknown application source')
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,note=?,updated=? WHERE id=?", (status, note, self.now(), identifier))
            if source is not None or status != 'applied':
                db.execute('UPDATE jobs SET application_source=? WHERE id=?',
                           (source if status == 'applied' else '', identifier))
            if job:
                db.execute("UPDATE jobs SET title=?,company=?,snapshot=? WHERE id=?", (job.get("title", ""), job.get("company", ""), json.dumps(job), identifier))
            if status == 'applied' and source == 'app_confirmed':
                db.execute('''INSERT OR IGNORE INTO confirmed_applications
                    SELECT id,COALESCE(NULLIF(submission_attempted_at,''),?),?,
                        CASE WHEN submission_attempted_at='' THEN 'confirmation' ELSE 'submission_attempt' END,
                        title,company,url FROM jobs WHERE id=?''', (self.now(), self.now(), identifier))

    def claim(self, identifier):
        # Persist before clicking Submit. Never automatically retry uncertain sends.
        with self.connect() as db:
            stamp = self.now()
            cursor = db.execute("UPDATE jobs SET status='submitting',updated=?,submission_attempted_at=? WHERE id=? AND status='queued'", (stamp, stamp, identifier))
            return cursor.rowcount == 1

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='unverified',note='App stopped during submission; check LinkedIn manually before retrying',updated=? WHERE status='submitting'", (self.now(),))
            db.execute("UPDATE application_runs SET outcome='interrupted',finished_at=?,detail='App restarted before this run finished' WHERE outcome='running'", (self.now(),))

    def requeue(self, identifier):
        with self.connect() as db:
            cursor = db.execute("UPDATE jobs SET status='queued',note='',retry_priority=1,updated=? WHERE id=? AND status IN ('needs_input','skipped','error')", (self.now(), identifier))
            return cursor.rowcount == 1

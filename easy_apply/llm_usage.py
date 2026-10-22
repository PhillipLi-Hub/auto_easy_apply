"""Local request history, including drafts that were never filled."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


LONDON = ZoneInfo('Europe/London')


def now():
    return datetime.now(timezone.utc).isoformat()


class UsageHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, started TEXT NOT NULL, day TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS requests_day ON requests(day,started)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, provider, model, job, question, kind, parent_id=''):
        identifier = uuid.uuid4().hex
        started = now()
        payload = dict(id=identifier, started=started, provider=provider, model=model,
                       requested_model=model, model_source='configured', question=question,
                       job_url=job.get('url', ''), title=job.get('title', ''), company=job.get('company', ''),
                       kind=kind, parent_id=parent_id, answer='', raw_response='', effective_answer='',
                       input_tokens=None, output_tokens=None, duration_seconds=None, detail='')
        with self.connect() as db:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                       (identifier, started, datetime.fromisoformat(started).astimezone(LONDON).date().isoformat(),
                        'running', json.dumps(payload)))
        return identifier

    def update(self, identifier, state, **values):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM requests WHERE id=?', (identifier,)).fetchone()
            payload = json.loads(row[0])
            payload.update(values, updated=now())
            db.execute('UPDATE requests SET state=?,payload=? WHERE id=?', (state, json.dumps(payload), identifier))

    def response(self, identifier, response, duration):
        raw = ''.join(part.get('text', '') for item in response.get('output', []) if item.get('type') == 'message'
                      for part in item.get('content', []) if part.get('type') == 'output_text')
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        usage = response.get('usage') or {}
        values = dict(raw_response=raw, answer=parsed.get('answer'), reason=parsed.get('reason', ''),
                      evidence=parsed.get('evidence_quotes', []), review=parsed if 'supported' in parsed else None,
                      input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                      duration_seconds=round(duration, 2), finished=now(), response_id=response.get('id'))
        if response.get('model'):
            values.update(model=response['model'], model_source='provider response')
        self.update(identifier, 'awaiting validation', **values)

    def rows(self, day=None, limit=None):
        sql, params = 'SELECT state,payload FROM requests', []
        if day:
            sql += ' WHERE day=?'
            params.append(day)
        sql += ' ORDER BY started DESC,id DESC'
        if limit:
            sql += ' LIMIT ?'
            params.append(limit)
        with self.connect() as db:
            return [json.loads(payload) | {'state': state} for state, payload in db.execute(sql, params)]

    def recover(self):
        # Call only at desktop startup while holding the single-instance lock.
        with self.connect() as db:
            db.execute("UPDATE requests SET state='interrupted' WHERE state IN ('running','awaiting validation')")

    def import_legacy(self, path):
        if not Path(path).exists():
            return
        with self.connect() as db, Path(path).open(encoding='utf-8') as source:
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                return
            first_new = db.execute("SELECT MIN(started) FROM requests WHERE id NOT LIKE 'legacy-%'").fetchone()[0]
            for index, line in enumerate(source):
                try:
                    record = json.loads(line)
                    started = record['at']
                    day = datetime.fromisoformat(started).astimezone(LONDON).date().isoformat()
                except (ValueError, KeyError, TypeError):
                    continue
                if first_new and started >= first_new:
                    continue
                identifier = 'legacy-' + hashlib.sha256(f'{index}:{line}'.encode()).hexdigest()
                payload = {key: record.get(key) for key in ('provider', 'model', 'question', 'answer', 'job_url', 'evidence', 'basis')}
                usage = record.get('usage') or {}
                payload.update(id=identifier, started=started, kind='historical answer',
                               input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                               model_source='historical log',
                               detail='Imported answer only. Earlier failed requests and evidence-review calls were not recorded.')
                state = 'historical rejected' if record.get('accepted') is False or record.get('basis') == 'rejected_after_manual_cv_review' else 'historical accepted'
                db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?)', (identifier, started, day, state, json.dumps(payload)))
            db.execute("INSERT INTO metadata VALUES('legacy_imported','1')")

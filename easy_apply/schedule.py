"""Once-per-local-day requests delivered to the single desktop instance."""

import json
from datetime import datetime


def schedule_due(data, now=None):
    """Catch up only after today's configured start time, never before it."""
    now = now or datetime.now().astimezone()
    try:
        config = json.loads((data / 'schedule.json').read_text(encoding='utf-8-sig'))
        hour, minute = map(int, config['time'].split(':'))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return False
        return bool(config.get('enabled')) and (now.hour, now.minute) >= (hour, minute)
    except (OSError, ValueError, TypeError, KeyError):
        return False


class ScheduleQueue:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS scheduled_runs (
                day TEXT PRIMARY KEY, requested_at TEXT NOT NULL,
                status TEXT NOT NULL, updated TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')''')

    def request(self, now=None):
        now = now or datetime.now().astimezone()
        day, stamp = now.date().isoformat(), now.isoformat(timespec='seconds')
        with self.store.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO scheduled_runs(day,requested_at,status,updated) VALUES(?,?,'pending',?)", (day, stamp, stamp))
        return day, bool(cursor.rowcount)

    def claim(self, busy=False, now=None):
        now = now or datetime.now().astimezone()
        day, stamp = now.date().isoformat(), now.isoformat(timespec='seconds')
        with self.store.connect() as db:
            db.execute("UPDATE scheduled_runs SET status='expired',updated=?,result='Missed request from an earlier day; not replayed' WHERE status='pending' AND day<?", (stamp, day))
            status = 'skipped' if busy else 'running'
            result = 'Another app operation was active; no overlapping run started' if busy else ''
            cursor = db.execute("UPDATE scheduled_runs SET status=?,updated=?,result=? WHERE day=? AND status='pending'", (status, stamp, result, day))
        return day if cursor.rowcount else None

    def finish(self, day, status, result):
        with self.store.connect() as db:
            db.execute("UPDATE scheduled_runs SET status=?,updated=?,result=? WHERE day=? AND status='running'",
                       (status, datetime.now().astimezone().isoformat(timespec='seconds'), result, day))

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE scheduled_runs SET status='interrupted',result='App closed before the scheduled run finished; not retried automatically' WHERE status='running'")

    def latest(self):
        with self.store.connect() as db:
            row = db.execute('SELECT day,status,result FROM scheduled_runs ORDER BY day DESC LIMIT 1').fetchone()
        return dict(zip(('day', 'status', 'result'), row)) if row else None

    def pending(self, day):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM scheduled_runs WHERE day=? AND status='pending'", (day,)).fetchone() is not None


def schedule_label(data):
    path = data / 'schedule.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if config.get('enabled'):
            return f"Daily auto apply: {config['time']} (computer's local time). Windows must be signed in."
    except (OSError, ValueError, KeyError):
        pass
    return ''

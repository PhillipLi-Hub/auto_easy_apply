"""Private hosted report sync. Networking runs separately from the application worker."""
import argparse
import getpass
import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .daily_report import build_report, local_day, report_days
from .report_mail import protect_secret

CONFIG = 'dashboard.json'
SECRET = 'dashboard_secret.bin'


def load_config(store):
    path = store.path.parent / CONFIG
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def valid_origin(value):
    url = urllib.parse.urlsplit(value.strip())
    if (url.scheme != 'https' or not url.hostname or not url.hostname.endswith('.chatgpt.site')
            or url.username or url.password or url.port not in (None, 443)
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Use the private dashboard HTTPS address ending in .chatgpt.site')
    return 'https://' + url.hostname


def configure(store, origin, token):
    origin = valid_origin(origin)
    if not token.strip():
        raise ValueError('Dashboard credential is required')
    secret = store.path.parent / SECRET
    temp = secret.with_suffix('.tmp')
    temp.write_bytes(protect_secret(json.dumps({'origin': origin, 'token': token.strip()}).encode()))
    temp.replace(secret)
    path = store.path.parent / CONFIG
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'origin': origin, 'enabled': True}, indent=2), encoding='utf-8')
    temp.replace(path)
    # Credentials may now point to a different destination. Resend all reports.
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS dashboard_synced(day TEXT PRIMARY KEY, digest TEXT NOT NULL)')
        db.execute('DELETE FROM dashboard_synced')


def credentials(store):
    config = load_config(store)
    if not config.get('enabled'):
        raise ValueError('Dashboard sync is disabled')
    origin = valid_origin(config['origin'])
    secret = json.loads(protect_secret((store.path.parent / SECRET).read_bytes(), decrypt=True).decode())
    if secret['origin'] != origin:
        raise ValueError('Dashboard address changed; reconnect it before uploading')
    return origin, secret['token']


def export_report(store, day):
    report = build_report(store, day)
    # Explicit allowlist: never export CVs, answers, notes, snapshots or LLM logs.
    result = {key: report[key] for key in ('date', 'generated_at', 'total_confirmed', 'counts', 'approximate_date_count')}
    result['applications'] = [
        {key: row[key] for key in ('job_id', 'title', 'company', 'category', 'local_time', 'timestamp_basis')}
        for row in report['applications']]
    result['runs'] = [
        {key: row[key] for key in ('id', 'started_at', 'finished_at', 'action', 'outcome')}
        for row in report['runs']]
    return result


def fingerprint(report):
    return hashlib.sha256(json.dumps({k:v for k,v in report.items() if k != 'generated_at'},
                                     sort_keys=True).encode()).hexdigest()


def runtime_snapshot(store, override=None):
    with store.connect() as db:
        last = db.execute('SELECT outcome,started_at FROM application_runs ORDER BY id DESC LIMIT 1').fetchone()
    current_run = last and (local_day(last[1]) == local_day() or last[0] == 'running')
    state = last[0] if current_run else 'idle'
    if state not in {'idle','running','paused','completed','daily_limit','stopped','error','interrupted'}:
        state = 'idle'
    rows = store.rows()
    runtime = {'updated_at': datetime.now(timezone.utc).isoformat(), 'state': state,
               'queued': sum(r['status'] == 'queued' for r in rows),
               'needs_attention': sum(r['status'] == 'needs_input' for r in rows),
               'unverified': sum(r['status'] in {'submitting','unverified'} for r in rows),
               'resume_at': None}
    if override and current_run:
        runtime.update(override)
    return runtime


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward service credentials to a sign-in page or another host.
        return None


def send(origin, token, payload):
    request = urllib.request.Request(origin + '/api/reports',
        data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json', 'OAI-Sites-Authorization':'Bearer '+token,
                 'X-Dashboard-Key':token}, method='POST')
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=12) as response:
        result = json.loads(response.read(100000))
    expected = {r['date'] for r in payload['reports']}
    if result.get('ok') is not True or set(result.get('accepted', [])) != expected:
        raise ValueError('Dashboard did not acknowledge the uploaded reports')


def sync_once(store, override=None, sender=send):
    origin, token = credentials(store)
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS dashboard_synced(day TEXT PRIMARY KEY, digest TEXT NOT NULL)')
        known = dict(db.execute('SELECT day,digest FROM dashboard_synced'))
    pending = []
    for day in report_days(store):
        report = export_report(store, day)
        digest = fingerprint(report)
        if known.get(day) != digest:
            pending.append((report, digest))
    batches = [pending[i:i+10] for i in range(0, len(pending), 10)] or [[]]
    for batch in batches:
        sender(origin, token, {'reports':[r for r,_ in batch], 'runtime':runtime_snapshot(store, override)})
        with store.connect() as db:
            db.executemany('INSERT INTO dashboard_synced(day,digest) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET digest=excluded.digest',
                           [(r['date'], digest) for r,digest in batch])
    return len(pending)


class DashboardPublisher(threading.Thread):
    def __init__(self, store, messages, runtime=lambda: None, interval=60):
        super().__init__(daemon=True)
        self.store, self.messages, self.runtime, self.interval = store, messages, runtime, interval
        self.done, self.wake = threading.Event(), threading.Event()

    def request_sync(self):
        self.wake.set()

    def close(self):
        self.done.set()
        self.wake.set()

    def run(self):
        while not self.done.is_set():
            self.wake.clear()
            try:
                if load_config(self.store).get('enabled'):
                    sync_once(self.store, self.runtime())
                    self.messages.put(('dashboard_status', 'Dashboard synced at '+datetime.now().strftime('%H:%M:%S')))
            except Exception as error:
                # Exception bodies can echo request headers. Emit only safe classes/status.
                if isinstance(error, urllib.error.HTTPError):
                    reason = 'authorization failed; reconnect the dashboard' if error.code in (301,302,303,307,308,401,403) else 'server returned HTTP '+str(error.code)
                else:
                    reason = 'connection or configuration unavailable'
                self.messages.put(('dashboard_status', 'Dashboard sync pending: '+reason+'. Will retry automatically.'))
            self.wake.wait(self.interval)


if __name__ == '__main__':
    from .store import Store
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--sync', action='store_true')
    args = parser.parse_args()
    store = Store()
    if args.configure:
        origin = input('Dashboard URL: ')
        token = getpass.getpass('Dashboard service credential (hidden): ')
        configure(store, origin, token)
        print('Dashboard configured. Credential protected for your Windows account.')
    if args.sync:
        print('Reports uploaded:', sync_once(store))

"""Daily-limit email delivery with an outbox and Windows-protected credentials."""

import csv
import ctypes
import io
import json
import os
import re
import smtplib
import ssl
import uuid
from ctypes import wintypes
from email.message import EmailMessage
from email.utils import formatdate

from .daily_report import summary, render_html


def protect_secret(value, decrypt=False):
    if os.name != 'nt':
        raise OSError('Gmail credential storage requires Windows')
    class Blob(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming, outgoing = Blob(len(value), buffer), Blob()
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError('Windows could not unlock the saved Gmail credential' if decrypt else 'Windows could not protect the Gmail credential')
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel.LocalFree(outgoing.data)


def config_path(store):
    return store.path.parent / 'report_email.json'


def load_config(store):
    path = config_path(store)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'enabled': False, 'address': ''}


def save_config(store, address, enabled, password=''):
    address = address.strip().lower()
    if not re.fullmatch(r'[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@(?:gmail|googlemail)\.com', address):
        raise ValueError('Enter your Gmail address')
    if password:
        password = ''.join(password.split())
        if not re.fullmatch(r'[A-Za-z0-9]{16}', password):
            raise ValueError('Enter the 16-character Google App Password, not your normal password')
        encrypted = protect_secret(json.dumps({'address': address, 'password': password}).encode('utf-8'))
        path = store.path.parent / 'gmail_report_secret.bin'
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(encrypted)
        temporary.replace(path)
    path = config_path(store)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'enabled': bool(enabled), 'address': address}, indent=2), encoding='utf-8')
    temporary.replace(path)


def credentials(store):
    config = load_config(store)
    if not config.get('enabled'):
        raise ValueError('Automatic report email is disabled')
    path = store.path.parent / 'gmail_report_secret.bin'
    if not path.exists():
        raise ValueError('Gmail setup required: enter a Google App Password in Daily reports → Gmail setup')
    saved = json.loads(protect_secret(path.read_bytes(), decrypt=True).decode('utf-8'))
    if saved['address'] != config.get('address'):
        raise ValueError('Save an App Password for the selected Gmail address')
    return saved['address'], saved['password']


def connect(address, password):
    client = smtplib.SMTP('smtp.gmail.com', 587, timeout=60)
    try:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(address, password)
        return client
    except Exception:
        client.close()
        raise


def safe_error(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'Gmail rejected the App Password. Check Gmail setup.'
    if isinstance(exc, ValueError):
        return str(exc)
    return f'Email delivery failed ({type(exc).__name__}). Check the connection and Gmail setup.'


def verify(store):
    address, password = credentials(store)
    client = connect(address, password)
    client.close()
    return f'Gmail sign-in verified for {address}'


def message(report, address, message_id):
    mail = EmailMessage()
    mail['From'] = address
    mail['To'] = address
    mail['Subject'] = f"Easy Apply daily report — {report['date']} ({report['total_confirmed']} confirmed)"
    mail['Message-ID'] = message_id
    mail['Date'] = formatdate(localtime=True)
    lines = ['LinkedIn’s daily Easy Apply limit was reached. Auto apply has stopped.', '', summary(report), '', report['counting_rule'], '', 'Confirmed roles:']
    lines.extend(f"{row['category']}: {row['title'] or row['job_id']} — {row['company']}\n{row['url']}" for row in report['applications'])
    mail.set_content('\n'.join(lines))
    mail.add_alternative(render_html(report), subtype='html')
    mail.add_attachment(render_html(report).encode('utf-8'), maintype='text', subtype='html', filename=f"applications-{report['date']}.html")
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['Role category', 'Confirmed applications'])
    writer.writerows(report['counts'].items())
    writer.writerow(['Total', report['total_confirmed']])
    mail.add_attachment(stream.getvalue().encode('utf-8-sig'), maintype='text', subtype='csv', filename=f"application-counts-{report['date']}.csv")
    return mail


class ReportOutbox:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS report_email_outbox (
                day TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL,
                message_id TEXT NOT NULL, recipient TEXT NOT NULL DEFAULT '',
                updated TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')''')

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE report_email_outbox SET state='uncertain',detail='App closed during delivery. Check Gmail Sent before attempting another send.' WHERE state='sending'")

    def enqueue(self, report):
        with self.store.connect() as db:
            return db.execute("INSERT OR IGNORE INTO report_email_outbox(day,state,payload,message_id,updated) VALUES(?,'pending',?,?,?)",
                              (report['date'], json.dumps(report), f'<{uuid.uuid4().hex}@easy-apply.local>', self.store.now())).rowcount == 1

    def latest_status(self):
        with self.store.connect() as db:
            row = db.execute('SELECT day,state,detail FROM report_email_outbox ORDER BY day DESC LIMIT 1').fetchone()
        return f'Email {row[0]}: {row[1]}. {row[2]}' if row else 'Email reports are sent when LinkedIn’s daily limit is reached.'

    def deliver(self):
        with self.store.connect() as db:
            pending = db.execute("SELECT day,payload,message_id FROM report_email_outbox WHERE state IN ('pending','failed') ORDER BY day").fetchall()
        results = []
        for day, raw, identifier in pending:
            with self.store.connect() as db:
                claimed = db.execute("UPDATE report_email_outbox SET state='sending',updated=? WHERE day=? AND state IN ('pending','failed')", (self.store.now(), day)).rowcount
            if not claimed:
                continue
            client, sending, address = None, False, ''
            try:
                address, password = credentials(self.store)
                mail = message(json.loads(raw), address, identifier)
                client = connect(address, password)
                sending = True
                refused = client.send_message(mail)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                state, detail = 'sent', f'Gmail accepted the report for {address}'
            except Exception as exc:
                # A timeout after DATA may mean Gmail accepted the message.
                # Do not automatically retry an uncertain send and duplicate it.
                state = 'uncertain' if sending and not isinstance(exc, smtplib.SMTPResponseException) and not isinstance(exc, smtplib.SMTPRecipientsRefused) else 'failed'
                detail = safe_error(exc)
                if state == 'uncertain':
                    detail += ' Delivery is uncertain; check Gmail Sent. Automatic retry is blocked.'
            finally:
                if client:
                    try:
                        client.close()
                    except OSError:
                        pass
            with self.store.connect() as db:
                db.execute('UPDATE report_email_outbox SET state=?,recipient=?,detail=?,updated=? WHERE day=?',
                           (state, address, detail, self.store.now(), day))
            results.append(f'Email {day}: {state}. {detail}')
        return results

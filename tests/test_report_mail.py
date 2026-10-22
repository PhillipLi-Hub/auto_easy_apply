import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from easy_apply.store import Store
from easy_apply.daily_report import build_report
from easy_apply.report_mail import ReportOutbox, save_config, credentials, message, connect


class ReportMailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'jobs.sqlite3')
        self.outbox = ReportOutbox(self.store)
        self.report = build_report(self.store, '2026-10-18')

    def state(self):
        with self.store.connect() as db:
            return db.execute('SELECT state FROM report_email_outbox').fetchone()[0]

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_credentials_are_encrypted_bound_to_address_and_not_in_config(self):
        password = 'abcdefghijklmnop'
        save_config(self.store, 'test@gmail.com', True, password)
        self.assertEqual(credentials(self.store), ('test@gmail.com', password))
        self.assertNotIn(password.encode(), (self.store.path.parent / 'gmail_report_secret.bin').read_bytes())
        self.assertNotIn(password, (self.store.path.parent / 'report_email.json').read_text())
        save_config(self.store, 'different@gmail.com', True)
        with self.assertRaisesRegex(ValueError, 'selected Gmail'):
            credentials(self.store)

    def test_only_one_delivery_for_repeated_limit_and_restart(self):
        self.assertTrue(self.outbox.enqueue(self.report))
        self.assertFalse(self.outbox.enqueue(self.report))
        smtp = Mock()
        smtp.send_message.return_value = {}
        with patch('easy_apply.report_mail.credentials', return_value=('test@gmail.com', 'fake')), patch('easy_apply.report_mail.connect', return_value=smtp):
            self.assertIn(': sent.', self.outbox.deliver()[0])
            ReportOutbox(self.store).recover()
            self.assertEqual(ReportOutbox(self.store).deliver(), [])
        smtp.send_message.assert_called_once()
        self.assertEqual(self.state(), 'sent')
        mail = smtp.send_message.call_args.args[0]
        self.assertEqual(mail['To'], 'test@gmail.com')
        self.assertEqual(mail['From'], mail['To'])
        self.assertEqual(len(list(mail.iter_attachments())), 2)
        self.assertIn('Quant: 0', mail.get_body(preferencelist=('plain',)).get_content())

    def test_missing_setup_is_retryable_and_does_not_open_smtp(self):
        self.outbox.enqueue(self.report)
        with patch('easy_apply.report_mail.connect') as smtp:
            self.assertIn('disabled', self.outbox.deliver()[0])
        smtp.assert_not_called()
        self.assertEqual(self.state(), 'failed')

    def test_auth_failure_is_retryable_without_leaking_response(self):
        self.outbox.enqueue(self.report)
        with patch('easy_apply.report_mail.credentials', return_value=('test@gmail.com', 'secret')), \
             patch('easy_apply.report_mail.connect', side_effect=smtplib.SMTPAuthenticationError(535, b'sensitive server text')):
            result = self.outbox.deliver()[0]
        self.assertEqual(self.state(), 'failed')
        self.assertNotIn('sensitive', result)
        self.assertNotIn('secret', result)

    def test_timeout_during_send_is_uncertain_and_not_retried(self):
        self.outbox.enqueue(self.report)
        smtp = Mock()
        smtp.send_message.side_effect = TimeoutError()
        with patch('easy_apply.report_mail.credentials', return_value=('test@gmail.com', 'fake')), patch('easy_apply.report_mail.connect', return_value=smtp):
            self.assertIn('uncertain', self.outbox.deliver()[0])
            self.assertEqual(self.outbox.deliver(), [])
        self.assertEqual(self.state(), 'uncertain')
        smtp.send_message.assert_called_once()

    def test_interrupted_delivery_is_not_replayed(self):
        self.outbox.enqueue(self.report)
        with self.store.connect() as db:
            db.execute("UPDATE report_email_outbox SET state='sending'")
        self.outbox.recover()
        self.assertEqual(self.state(), 'uncertain')
        self.assertEqual(self.outbox.deliver(), [])

    def test_verified_tls_precedes_authentication(self):
        smtp = Mock()
        with patch('easy_apply.report_mail.smtplib.SMTP', return_value=smtp) as factory:
            self.assertIs(connect('test@gmail.com', 'fake'), smtp)
        factory.assert_called_once_with('smtp.gmail.com', 587, timeout=60)
        self.assertEqual([call[0] for call in smtp.method_calls], ['ehlo', 'starttls', 'ehlo', 'login'])
        self.assertTrue(smtp.starttls.call_args.kwargs['context'].check_hostname)

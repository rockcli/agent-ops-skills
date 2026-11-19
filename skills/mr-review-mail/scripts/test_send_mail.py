"""Offline behavioral checks. Every SMTP transport and credential is fake."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import send_mail


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.draft = {'to': 'author@example.com', 'subject': '原则', 'body': '先校验，再改变状态。'}
        self.path = self.root / 'draft.json'
        self.path.write_text(json.dumps(self.draft))
        self.receipt = self.root / 'receipt.json'
        self.env = {'ali_mail_from': 'sender@example.com', 'ali_mail_key': 'FAKE_SECRET_NEVER_PRINT', 'ali_mail_smtp_host': 'smtp.example.com', 'ali_mail_smtp_port': '465'}

    def transport(self, failure=None, quit_failure=None):
        client = MagicMock()
        client.__enter__.return_value = client
        client.send_message.return_value = {}
        if failure:
            client.send_message.side_effect = failure
        if quit_failure:
            client.__exit__.side_effect = quit_failure
        return client

    def test_preview_does_not_read_credentials_or_connect(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL') as ssl, patch.object(send_mail.smtplib, 'SMTP') as plain, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(send_mail.main(['--draft', str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'preview_only')
        ssl.assert_not_called()
        plain.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_oversize_and_header_injection_rejected(self):
        for replacement in [{'body': '字' * 501}, {'subject': '主题\nBcc: other@example.com'}, {'to': 'author@example.com\r\nBcc: other@example.com'}]:
            self.path.write_text(json.dumps({**self.draft, **replacement}))
            with self.assertRaises(send_mail.InputError):
                send_mail.load_draft(self.path, 500)

    def test_success_is_durable_and_duplicate_attempt_blocked(self):
        client = self.transport()
        with patch.dict(os.environ, self.env, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL', return_value=client) as connector:
            record = send_mail.send(self.draft, self.receipt)
            with self.assertRaises(send_mail.InputError):
                send_mail.send(self.draft, self.receipt)
        self.assertEqual(record['status'], 'smtp_accepted')
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'smtp_accepted')
        self.assertNotIn(self.env['ali_mail_key'], self.receipt.read_text())
        connector.assert_called_once()
        client.send_message.assert_called_once()
        self.assertEqual(client.send_message.call_args.kwargs['to_addrs'], ['author@example.com'])

    def test_auth_failure_is_before_send_and_secret_not_logged(self):
        client = self.transport()
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, b'FAKE_SECRET_NEVER_PRINT')
        with patch.dict(os.environ, self.env, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL', return_value=client), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            result = send_mail.main(['--draft', str(self.path), '--send', '--receipt', str(self.receipt)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())['status'], 'failed_before_send')
        self.assertNotIn(self.env['ali_mail_key'], output.getvalue() + error.getvalue() + self.receipt.read_text())
        client.send_message.assert_not_called()

    def test_connection_loss_during_send_is_unknown_and_not_retried(self):
        client = self.transport(smtplib.SMTPServerDisconnected('lost after DATA'))
        with patch.dict(os.environ, self.env, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL', return_value=client):
            self.assertEqual(send_mail.send(self.draft, self.receipt)['status'], 'outcome_unknown')
            with self.assertRaises(send_mail.InputError):
                send_mail.send(self.draft, self.receipt)
        client.send_message.assert_called_once()

    def test_explicit_data_rejection_is_not_unknown(self):
        client = self.transport(smtplib.SMTPDataError(550, b'rejected'))
        with patch.dict(os.environ, self.env, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL', return_value=client):
            self.assertEqual(send_mail.send(self.draft, self.receipt)['status'], 'rejected')

    def test_quit_failure_does_not_turn_accepted_into_failure(self):
        client = self.transport(quit_failure=smtplib.SMTPServerDisconnected('quit failed'))
        with patch.dict(os.environ, self.env, clear=True), patch.object(send_mail.smtplib, 'SMTP_SSL', return_value=client):
            self.assertEqual(send_mail.send(self.draft, self.receipt)['status'], 'smtp_accepted')

    def test_starttls_before_authentication(self):
        client = self.transport()
        with patch.dict(os.environ, {**self.env, 'ali_mail_smtp_port': '587'}, clear=True), patch.object(send_mail.smtplib, 'SMTP', return_value=client), patch.object(send_mail.smtplib, 'SMTP_SSL') as ssl:
            self.assertEqual(send_mail.send(self.draft, self.receipt)['status'], 'smtp_accepted')
        ssl.assert_not_called()
        calls = [call[0] for call in client.mock_calls]
        self.assertLess(calls.index('starttls'), calls.index('login'))


if __name__ == '__main__':
    unittest.main()

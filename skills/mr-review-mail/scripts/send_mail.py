"""Preview or send one authorized short email; never log SMTP secrets."""
import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
import hashlib
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys


class InputError(ValueError):
    """Safe, locally authored validation messages, never raw server responses."""


def address(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n'):
        raise InputError('Invalid email address')
    value = value.strip()
    _, parsed = parseaddr(value)
    if parsed != value or parsed.count('@') != 1 or not all(parsed.split('@')) or any(c.isspace() for c in parsed):
        raise InputError('Use one verified plain email address')
    return parsed


def load_draft(path, max_chars):
    draft = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(draft, dict) or set(draft) != {'to', 'subject', 'body'}:
        raise InputError('Draft requires only to, subject and body')
    draft['to'] = address(draft['to'])
    for field in ['subject', 'body']:
        if not isinstance(draft[field], str) or not draft[field].strip():
            raise InputError('Subject and body must be nonempty strings')
    if any(c in draft['subject'] for c in '\r\n'):
        raise InputError('Subject must be one line')
    if max_chars <= 0 or len(draft['subject'] + draft['body']) > max_chars:
        raise InputError('Subject plus body exceeds the requested character limit')
    return draft


def save_receipt(path, record):
    # Atomic replacements preserve a readable state if the process is interrupted.
    temporary = path.with_name(path.name + '.writing')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def send(draft, receipt):
    names = ['ali_mail_from', 'ali_mail_key', 'ali_mail_smtp_host', 'ali_mail_smtp_port']
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise InputError('Missing environment variables: ' + ', '.join(missing))
    sender = address(os.environ['ali_mail_from'])
    host = os.environ['ali_mail_smtp_host'].strip()
    if not host or any(c.isspace() for c in host):
        raise InputError('Invalid SMTP host')
    try:
        port = int(os.environ['ali_mail_smtp_port'])
    except ValueError:
        raise InputError('Invalid SMTP port') from None
    if not 1 <= port <= 65535:
        raise InputError('Invalid SMTP port')
    message = EmailMessage()
    message['From'], message['To'] = sender, draft['to']
    message['Subject'] = draft['subject']
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid(domain=sender.split('@')[1])
    message.set_content(draft['body'])
    record = {
        'status': 'prepared', 'from': sender, 'to': draft['to'],
        'subject': draft['subject'], 'message_id': message['Message-ID'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'draft_sha256': hashlib.sha256(json.dumps(draft, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }
    receipt = Path(receipt)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a previous attempt, including ambiguous/interrupted sends.
    try:
        with receipt.open('x', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise InputError('Receipt already exists; reconcile that attempt before any resend') from None
    stage = 'connect'
    try:
        tls = ssl.create_default_context()
        connector = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        kwargs = {'timeout': 30}
        if port == 465:
            kwargs['context'] = tls
        with connector(host, port, **kwargs) as smtp:
            if port != 465:
                smtp.ehlo()
                smtp.starttls(context=tls)
                smtp.ehlo()
            stage = 'authenticate'
            smtp.login(sender, os.environ['ali_mail_key'])
            record['status'] = 'sending'
            save_receipt(receipt, record)
            stage = 'send'
            refused = smtp.send_message(message, from_addr=sender, to_addrs=[draft['to']])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            record['status'] = 'smtp_accepted'
            save_receipt(receipt, record)
            stage = 'quit'
    except Exception as exc:
        if record['status'] != 'smtp_accepted':
            explicit_rejection = isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError, smtplib.SMTPSenderRefused))
            record['status'] = ('rejected' if explicit_rejection else 'outcome_unknown') if stage == 'send' else 'failed_before_send'
            record['error_type'] = type(exc).__name__
            record['smtp_code'] = getattr(exc, 'smtp_code', None)
        # No exception text or server response body: either may contain credentials.
        try:
            save_receipt(receipt, record)
        except OSError:
            record['receipt_update_failed'] = True
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--draft', required=True)
    parser.add_argument('--max-chars', type=int, default=500)
    parser.add_argument('--send', action='store_true', help='Use only with explicit user authorization')
    parser.add_argument('--receipt')
    args = parser.parse_args(argv)
    try:
        draft = load_draft(args.draft, args.max_chars)
        if not args.send:
            print(json.dumps({'status': 'preview_only', **draft, 'characters': len(draft['subject'] + draft['body'])}, ensure_ascii=False, indent=2))
            return 0
        if not args.receipt:
            raise InputError('--send requires --receipt')
        record = send(draft, args.receipt)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0 if record['status'] == 'smtp_accepted' else 1
    except InputError as exc:
        print(json.dumps({'status': 'not_sent', 'error': str(exc)}), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # Input errors are local; avoid printing file contents or raw SMTP errors.
        print(json.dumps({'status': 'not_sent', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

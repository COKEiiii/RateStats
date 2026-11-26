"""Build and send mail for a verified four-workbook delivery."""
import fcntl
import hashlib
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

import certifi

from .common import digest, load, save
from .delivery import verify_delivery

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/email.local.json'
DEFAULTS = dict(enabled=False, smtp_host='', smtp_port=465, tls='ssl',
                smtp_user='', smtp_password='', from_email='', to_emails='', cc_emails='',
                subject_template='市场利率结果 {date}', body_template='您好，\n\n请查收市场利率结果。',
                delivery_mode='single')
DELIVERY_MODES = {
    'single': '一封邮件（四张表）',
    'both': '两封邮件（每封两张表）',
}
FIELDS = dict(raw_report='report', raw_rainbow='rainbow',
              formatted_report='formatted_report', formatted_rainbow='formatted_rainbow')
SPLIT_PARTS = {
    'raw': ('全量报表（1/2）', ('raw_report', 'raw_rainbow')),
    'formatted': ('格式对齐报表（2/2）', ('formatted_report', 'formatted_rainbow')),
}
SPLIT_ATTACHMENT_LIMIT = 10_000_000
UNCONFIRMED = {'partial', 'unknown', 'pending'}
SOURCES = dict(sgd='新元促销', board='挂牌', fx='外币促销')
SGT = timezone(timedelta(hours=8))
SMTP_CONNECT_TIMEOUT = 30
SMTP_ENVELOPE_TIMEOUT = 120
SMTP_UPLOAD_TIMEOUT = 300
SMTP_FINAL_REPLY_TIMEOUT = 600


class _SubmissionPhases:
    """Track DATA boundaries while smtplib handles framing and the envelope."""

    submission_phase = 'envelope'

    def data(self, message):
        self.submission_phase = 'data_command'
        return super().data(message)

    def getreply(self):
        code, reply = super().getreply()
        if self.submission_phase == 'data_command' and code == 354:
            self.submission_phase = 'data_upload'
            self.sock.settimeout(SMTP_UPLOAD_TIMEOUT)
        return code, reply

    def send(self, data):
        super().send(data)
        if self.submission_phase == 'data_upload':
            self.submission_phase = 'data_reply'
            self.sock.settimeout(SMTP_FINAL_REPLY_TIMEOUT)


class _SubmissionSMTP(_SubmissionPhases, smtplib.SMTP):
    pass


class _SubmissionSMTPSSL(_SubmissionPhases, smtplib.SMTP_SSL):
    pass


def read_config(path=CONFIG):
    return {**DEFAULTS, **load(path)} if path.exists() else DEFAULTS.copy()


def save_config(config, path=CONFIG):
    validate_config(config, require_ready=config['enabled'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf8', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def import_legacy(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith(('#', ';')) and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    config = {**DEFAULTS, **{key: values[key] for key in DEFAULTS if key in values}}
    config['enabled'] = False
    config['tls'] = ('ssl' if values.get('smtp_ssl', 'true').lower() in
                     {'1', 'true', 'yes', 'y', 'on'} else 'starttls')
    config['smtp_port'] = values.get('smtp_port') or (465 if config['tls'] == 'ssl' else 587)
    config['body_template'] = config['body_template'].replace(r'\n', '\n')
    return config


def addresses(value):
    if '\n' in value or '\r' in value:
        raise ValueError('邮箱地址不能包含换行。')
    result = []
    seen = set()
    for item in value.replace(';', ',').split(','):
        item = item.strip()
        if not item:
            continue
        if not re.fullmatch(r"[^\s@<>,;:\x00-\x1f]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*", item) or not item.isascii():
            raise ValueError('请输入有效的邮箱地址，多个地址用逗号或分号分隔。')
        if item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return result


def validate_config(config, *, test_to=None, require_ready=True):
    if config.get('delivery_mode', DEFAULTS['delivery_mode']) not in tuple(DELIVERY_MODES):
        raise ValueError('请选择一封或两封邮件的发送方式。')
    if config['tls'] not in ('ssl', 'starttls'):
        raise ValueError('请选择 SSL 或 STARTTLS。')
    try:
        port = int(config['smtp_port'])
    except (ValueError, TypeError):
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError('SMTP 端口必须介于 1 和 65535。')
    if any(c in config['subject_template'] for c in '\r\n'):
        raise ValueError('邮件标题不能包含换行。')
    sender = addresses(config['from_email'] or config['smtp_user'])
    to = addresses(test_to if test_to is not None else config['to_emails'])
    cc = [] if test_to is not None else addresses(config['cc_emails'])
    if test_to is not None and (len(to) != 1 or to[0] != test_to.strip()):
        raise ValueError('测试发送只允许一个邮箱地址。')
    if require_ready and (not config['smtp_host'].strip() or not to or len(sender) != 1):
        raise ValueError('请在“邮件设置”填写 SMTP 主机、发件人和收件人。')
    if require_ready and config['smtp_user'] and not config['smtp_password']:
        raise ValueError('请填写 SMTP 密码或授权码。')
    return (sender[0] if sender else ''), to, [
        addr for addr in cc if addr.casefold() not in {item.casefold() for item in to}
    ]


@dataclass(frozen=True)
class Delivery:
    work: Path
    output: Path
    as_of: str
    source_dates: dict
    rebuilt: bool
    files: dict
    hashes: dict


def verified_batch(workflow, project_root=ROOT):
    root = Path(project_root).resolve()
    work = Path(workflow).expanduser().resolve()
    if work.name == 'workflow.json':
        work = work.parent
    if not work.is_relative_to(root / 'runs'):
        raise ValueError('请选择本项目 runs 目录内的完整批次。')
    state = load(work / 'workflow.json')
    if Path(state.get('project', '')).resolve() != root or state.get('status') != 'complete':
        raise ValueError('只能发送本项目已完成的完整批次。')
    output = Path(state['output']).resolve()
    if not output.is_relative_to(root / 'outputs'):
        raise ValueError('交付目录必须位于本项目 outputs 内。')
    # Confine paths before verify_delivery reads any file.
    receipt = load(output / 'delivery.json')
    paths = {key: Path(path).resolve() for key, path in receipt['files'].items()}
    if (set(paths) != set(FIELDS) or len(set(paths.values())) != len(FIELDS)
            or any(path.parent != output or path.suffix.lower() != '.xlsx' for path in paths.values())):
        raise ValueError('四份附件必须是同一批次交付目录中的不同 Excel 文件。')
    verified = verify_delivery(output)
    saved = state.get('results', {}).get('delivery', {})
    if (verified['files'] != receipt['files'] or saved.get('files') != verified['files']
            or saved.get('hashes') != verified['hashes'] or state.get('output_hashes') != verified['hashes']
            or any(Path(state.get(field, '')).resolve() != paths[key] for key, field in FIELDS.items())):
        raise ValueError('工作流与四份交付文件的记录不一致。')
    source_dates = {key: date.fromisoformat(state['source_dates'][key]).isoformat() for key in SOURCES}
    return Delivery(work, output, date.fromisoformat(verified['as_of']).isoformat(), source_dates,
                    state.get('mode') == 'rebuild' or bool(state.get('delivery_revisions')),
                    paths, verified['hashes'])


def render(config, batch, *, test=False, part=None):
    label, keys = SPLIT_PARTS[part] if part else ('', tuple(FIELDS))
    source_dates = '；'.join(f'{label} {batch.source_dates[key]}' for key, label in SOURCES.items())
    attachments = '\n'.join(f'- {batch.files[key].name}' for key in keys)
    values = dict(date=batch.as_of.replace('-', ''), batch=batch.work.name, source_dates=source_dates,
                  attachments=attachments, market_name=batch.files[keys[0]].name,
                  rainbow_name=batch.files[keys[1]].name)

    def substitute(template):
        return re.sub(r'\{([a-z_]+)\}', lambda match: values.get(match[1], match[0]), template)

    subject = substitute(config['subject_template'])
    if label:
        subject += '｜' + label
    if test:
        subject = '【测试】' + subject
    rebuilt = '\n本批为重建结果，沿用原采集资料。' if batch.rebuilt else ''
    body = (f"{substitute(config['body_template']).rstrip()}\n\n批次：{batch.work.name}"
            f"\n资料日期：{source_dates}{rebuilt}\n\n附件：\n{attachments}\n")
    return subject, body


def _read_attachments(batch):
    data = {}
    for key in FIELDS:
        path = batch.files[key]
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != batch.hashes[key]:
            raise ValueError('附件在准备发送期间被修改：' + path.name)
        data[key] = content
    return data


def _check_split_size(label, size):
    if size >= SPLIT_ATTACHMENT_LIMIT:
        raise ValueError(f'{label}附件共 {size:,} 字节，必须低于 {SPLIT_ATTACHMENT_LIMIT:,} 字节。')


def _message_jobs(config, batch, split, *, test=False, example=False):
    mode = split if split is not None else config.get('delivery_mode', DEFAULTS['delivery_mode'])
    if mode not in (*DELIVERY_MODES, *SPLIT_PARTS):
        raise ValueError('未识别的邮件发送方式。')
    parts = tuple(SPLIT_PARTS) if mode == 'both' else (None if mode == 'single' else mode,)
    jobs = []
    for part in parts:
        label, keys = SPLIT_PARTS[part] if part else ('', tuple(FIELDS))
        subject, body = render(config, batch, test=test, part=part)
        size = None
        if part and not example:
            size = sum(batch.files[key].stat().st_size for key in keys)
            _check_split_size(label, size)
        jobs.append(dict(part=part, label=label, keys=keys, subject=subject, body=body, size=size))
    return jobs


def _format_preview(sender, to, cc, jobs):
    lines = [f"发件人：{sender or '未填写'}", f"收件人：{', '.join(to) or '未填写'}",
             f"抄送：{', '.join(cc) or '无'}"]
    for job in jobs:
        lines.append('主题：' + job['subject'])
        if job['size'] is not None:
            lines.append(f"附件合计：{job['size']:,} 字节")
        lines.extend(['', job['body']])
    return '\n'.join(lines)


def preview_config(config, batch, *, example=False):
    sender, to, cc = validate_config(config, require_ready=False)
    jobs = _message_jobs(config, batch, None, example=example)
    return _format_preview(sender, to, cc, jobs)


def _send_part(config, batch, folder, job, data, sender, to, cc, *, test):
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = sender, ', '.join(to), job['subject']
    if cc:
        message['Cc'] = ', '.join(cc)
    now = datetime.now(SGT)
    message['Date'] = format_datetime(now)
    message['Message-ID'] = make_msgid(domain=sender.rsplit('@', 1)[-1])
    message.set_content(job['body'])
    for key in job['keys']:
        message.add_attachment(data[key], maintype='application',
                               subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                               filename=batch.files[key].name)
    record = dict(status='pending', time=now.isoformat(), batch=batch.work.name, test=test,
                  to=to, cc=cc, subject=job['subject'], attachment_hashes=job['hashes'],
                  message_id=message['Message-ID'], fingerprint=job['fingerprint'])
    if job['part']:
        record.update(part=job['part'], attachment_bytes=job['size'])
    log = folder / (now.strftime('%Y%m%d-%H%M%S-%f') + '.json')
    save(log, record)
    started = monotonic()
    record.update(_submit(config, message, sender, to + cc),
                  elapsed_seconds=round(monotonic() - started, 2))
    save(log, record)
    return {**record, 'record': str(log)}


def send_mail(config, batch, confirm, *, test_to=None, dry_run=False, split=None, no_cc=False):
    """Confirm the exact envelope and payload under the batch send lock."""
    if not (config['enabled'] or test_to is not None or dry_run):
        return {'status': 'disabled'}
    sender, to, cc = validate_config(dict(config, cc_emails='') if no_cc else config, test_to=test_to)
    test = test_to is not None
    envelope = [str(batch.output), sender, sorted(a.casefold() for a in to),
                sorted(a.casefold() for a in cc)]
    jobs = _message_jobs(config, batch, split, test=test)
    both_parts = len(jobs) == len(SPLIT_PARTS)
    for job in jobs:
        part = job['part']
        hashes = {key: batch.hashes[key] for key in job['keys']}
        if part:
            fingerprint = digest([*envelope, part, hashes, test])
        else:
            fingerprint = digest([envelope[0], hashes, *envelope[1:], job['subject'], job['body']])
        job.update(hashes=hashes, fingerprint=fingerprint)
    preview = _format_preview(sender, to, cc, jobs)
    if dry_run:
        return {'status': 'preview', 'preview': preview}
    folder = batch.work / 'email-attempts'
    folder.mkdir(exist_ok=True, mode=0o700)
    with (folder / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy'}
        previous = [(path, load(path)) for path in sorted(folder.glob('*.json'), reverse=True)]
        results, pending = {}, []
        duplicate = False
        preview_notes = []
        for job in jobs:
            part = job['part']
            history = [(path, entry) for path, entry in previous
                       if entry['fingerprint'] == job['fingerprint']]
            accepted = next((path for path, entry in history if entry['status'] == 'accepted'), None)
            unconfirmed = next((path for path, entry in history if entry['status'] in UNCONFIRMED), None)
            if both_parts and (accepted or unconfirmed):
                status = 'already_accepted' if accepted else 'needs_review'
                results[part] = dict(status=status, part=part, record=str(accepted or unconfirmed))
                reason = '已被 SMTP 接受，本次跳过' if accepted else f'需核对收件情况后用 --split {part} 单独补发'
                preview_notes.append(f"{job['label']}：{reason}")
            else:
                pending.append(job)
                duplicate = duplicate or bool(accepted or unconfirmed)
        if pending and not confirm('\n'.join([preview, *preview_notes]), duplicate):
            return {'status': 'cancelled'}
        if pending:
            # Freeze and verify the entire delivery before opening either connection.
            data = _read_attachments(batch)
            for job in jobs:
                if job['part']:
                    _check_split_size(job['label'], sum(len(data[key]) for key in job['keys']))
            for job in pending:
                results[job['part']] = _send_part(config, batch, folder, job, data, sender, to, cc, test=test)
        if not both_parts:
            return results[jobs[0]['part']]
        ordered = [results[job['part']] for job in jobs]
        successful = sum(result['status'] in {'accepted', 'already_accepted'} for result in ordered)
        if successful == len(jobs):
            status = 'accepted'
        elif successful or any(result['status'] == 'partial' for result in ordered):
            status = 'partial'
        elif any(result['status'] in {'unknown', 'needs_review'} for result in ordered):
            status = 'unknown'
        else:
            status = 'failed'
        return dict(status=status, parts=ordered)


def _submit(config, message, sender, recipients):
    client = None
    phase = 'connect'
    try:
        context = ssl.create_default_context()
        # The macOS Python framework can ship without an installed CA bundle.
        context.load_verify_locations(cafile=certifi.where())
        address = config['smtp_host'].strip(), int(config['smtp_port'])
        if config['tls'] == 'ssl':
            client = _SubmissionSMTPSSL(*address, context=context, timeout=SMTP_CONNECT_TIMEOUT)
        else:
            client = _SubmissionSMTP(*address, timeout=SMTP_CONNECT_TIMEOUT)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if config['smtp_user']:
            phase = 'authenticate'
            client.login(config['smtp_user'], config['smtp_password'])
        client.sock.settimeout(SMTP_ENVELOPE_TIMEOUT)
        phase = 'envelope'
        client.submission_phase = phase
        refused = client.send_message(message, from_addr=sender, to_addrs=recipients)
        return dict(status='partial' if refused else 'accepted',
                    phase='data_reply', smtp_code=250,
                    rejected={address: int(reply[0]) for address, reply in refused.items()})
    except smtplib.SMTPRecipientsRefused as exc:
        return dict(status='failed', phase=phase, error='recipients_refused',
                    rejected={address: int(reply[0]) for address, reply in exc.recipients.items()})
    except smtplib.SMTPResponseException as exc:
        current = client.submission_phase if phase == 'envelope' else phase
        # A real negative SMTP reply establishes rejection. A malformed final
        # reply after sending the DATA terminator leaves acceptance unknown.
        uncertain = current == 'data_reply' and (
            not isinstance(exc, smtplib.SMTPDataError) or not 400 <= exc.smtp_code <= 599
        )
        return dict(status='unknown' if uncertain else 'failed', phase=current,
                    error=type(exc).__name__, smtp_code=exc.smtp_code)
    except (OSError, smtplib.SMTPException) as exc:
        current = client.submission_phase if phase == 'envelope' else phase
        result = dict(status='unknown' if current in ('data_upload', 'data_reply') else 'failed',
                      phase=current, error=type(exc).__name__)
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, OSError):
            result['transport_error'] = type(cause).__name__
        return result
    finally:
        if client is not None:
            # A QUIT/close failure cannot undo an acknowledged submission.
            try:
                client.close()
            except OSError:
                pass

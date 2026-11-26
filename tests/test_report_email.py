from contextlib import redirect_stdout
import fcntl
from io import BytesIO, StringIO
from pathlib import Path
import smtplib
import ssl
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from market_rates.common import load, save
from market_rates.report_email import (
    DEFAULTS, FIELDS, SMTP_ENVELOPE_TIMEOUT, SMTP_FINAL_REPLY_TIMEOUT, SMTP_UPLOAD_TIMEOUT,
    _SubmissionSMTP, import_legacy, preview_config, read_config, save_config, send_mail, verified_batch,
)
from market_rates.table_validation import sha
from scripts.email_send_gui import batch_choices, filter_batches
from scripts.send_report_email import main as send_main, offer_after_completion, show_result


class ReportEmailTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / 'runs/all-test'
        self.output = self.root / 'outputs/delivery'
        self.work.mkdir(parents=True)
        self.output.mkdir(parents=True)
        files = {}
        hashes = {}
        for key, name in zip(FIELDS, ['全量调研', '全量彩虹表', '格式对齐调研', '格式对齐彩虹表']):
            path = self.output / (name + '.xlsx')
            path.write_bytes(key.encode())
            files[key], hashes[key] = str(path), sha(path)
        receipt = dict(status='complete', as_of='2026-11-06', files=files, hashes=hashes)
        save(self.output / 'delivery.json', receipt)
        self.manifest = self.work / 'workflow.json'
        self.state = dict(project=str(self.root), status='complete', mode='rebuild',
                          output=str(self.output), output_hashes=hashes,
                          source_dates=dict(sgd='2026-11-06', board='2026-11-05', fx='2026-11-06'),
                          results=dict(delivery=receipt),
                          **{field: files[key] for key, field in FIELDS.items()})
        save(self.manifest, self.state)
        self.batch = verified_batch(self.work, self.root)
        self.config = dict(DEFAULTS, enabled=True, smtp_host='smtp.example.com',
                           smtp_user='sender@example.com', smtp_password='mock-secret',
                           to_emails='one@example.com;two@example.com,one@example.com',
                           cc_emails='cc@example.com;two@example.com')
        self.confirm = Mock(return_value=True)

    def test_import_and_save_legacy_settings_without_enabling(self):
        legacy = self.root / 'email_params.log'
        original = ('# legacy\nemail_enabled=true\nsmtp_ssl=false\nsmtp_user=sender@example.com\n'
                    'smtp_password=mock-secret\nbody_template=您好\\n{market_name}\n')
        legacy.write_text(original, encoding='utf-8-sig')
        config = import_legacy(legacy)
        self.assertFalse(config['enabled'])
        self.assertEqual(config['tls'], 'starttls')
        self.assertEqual(config['smtp_port'], 587)
        self.assertEqual(config['body_template'], '您好\n{market_name}')
        target = self.root / 'config/email.local.json'
        save_config(config, target)
        self.assertEqual(read_config(target), config)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(legacy.read_text(encoding='utf-8-sig'), original)

    @patch('market_rates.report_email._SubmissionSMTPSSL')
    def test_test_mail_has_only_test_recipient_and_exact_attachment_bytes(self, smtp):
        smtp.return_value.send_message.return_value = {}
        original = self.manifest.read_bytes()
        result = send_mail(dict(self.config, enabled=False), self.batch, self.confirm,
                           test_to='test@example.com')
        self.assertEqual(result['status'], 'accepted')
        message, = smtp.return_value.send_message.call_args.args
        self.assertEqual(smtp.return_value.send_message.call_args.kwargs['to_addrs'], ['test@example.com'])
        self.assertEqual(message['To'], 'test@example.com')
        self.assertNotIn('Cc', message)
        self.assertIn('【测试】', message['Subject'])
        self.assertIn('沿用原采集资料', message.get_body().get_content())
        self.assertIn('挂牌 2026-11-05', message.get_body().get_content())
        self.assertEqual([(part.get_filename(), part.get_payload(decode=True)) for part in message.iter_attachments()],
                         [(self.batch.files[key].name, key.encode()) for key in FIELDS])
        context = smtp.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertNotIn('mock-secret', Path(result['record']).read_text())

    @patch('market_rates.report_email._SubmissionSMTP')
    def test_starttls_and_partial_rejection(self, smtp):
        client = smtp.return_value
        client.send_message.return_value = {'cc@example.com': (550, b'not available')}
        result = send_mail(dict(self.config, tls='starttls', smtp_port=587), self.batch, self.confirm)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['rejected'], {'cc@example.com': 550})
        self.assertEqual([call[0] for call in client.mock_calls],
                         ['ehlo', 'starttls', 'ehlo', 'login', 'sock.settimeout', 'send_message', 'close'])
        self.assertGreater(client.sock.settimeout.call_args.args[0], smtp.call_args.kwargs['timeout'])
        self.assertEqual(client.send_message.call_args.kwargs['to_addrs'],
                         ['one@example.com', 'two@example.com', 'cc@example.com'])

    @patch('market_rates.report_email._SubmissionSMTPSSL')
    def test_failures_do_not_report_success_or_leak_smtp_response(self, smtp):
        client = smtp.return_value
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, b'mock-secret')
        first = send_mail(self.config, self.batch, self.confirm)
        self.assertEqual((first['status'], first['smtp_code']), ('failed', 535))
        self.assertEqual(first['phase'], 'authenticate')
        self.assertNotIn('mock-secret', Path(first['record']).read_text())
        client.send_message.assert_not_called()
        client.login.side_effect = None
        client.send_message.side_effect = TimeoutError()
        second = send_mail(self.config, self.batch, self.confirm)
        self.assertEqual(second['status'], 'failed')
        self.assertEqual(second['phase'], 'envelope')
        self.assertEqual(load(self.manifest)['status'], 'complete')

    def test_data_timeouts_and_disconnects_at_protocol_boundaries(self):
        envelope = b'250 sender\r\n250 first\r\n250 second\r\n'
        ready = b'354 upload\r\n'
        cases = [
            ('before upload', envelope, 'failed', 'data_command', None),
            ('waiting for acceptance', envelope + ready, 'unknown', 'data_reply', None),
            ('rejected', envelope + ready + b'554 rejected\r\n250 reset\r\n', 'failed', 'data_reply', 554),
            ('accepted', envelope + ready + b'250 accepted\r\n', 'accepted', 'data_reply', 250),
        ]
        config = dict(self.config, smtp_user='', from_email='sender@example.com', cc_emails='')
        for label, replies, status, phase, code in cases:
            with self.subTest(label=label):
                client = _SubmissionSMTP()
                client.ehlo_resp = b'OK'
                sock = client.sock = Mock()
                client.file = BytesIO(replies)
                with patch('market_rates.report_email._SubmissionSMTPSSL', return_value=client):
                    result = send_mail(config, self.batch, self.confirm)
                self.assertEqual((result['status'], result['phase'], result.get('smtp_code')), (status, phase, code))
                self.assertIn('elapsed_seconds', result)
                timeouts = [call.args[0] for call in sock.settimeout.call_args_list]
                expected = [SMTP_ENVELOPE_TIMEOUT]
                if ready in replies:
                    expected.extend([SMTP_UPLOAD_TIMEOUT, SMTP_FINAL_REPLY_TIMEOUT])
                self.assertEqual(timeouts, expected)
                sent = [call.args[0] for call in sock.sendall.call_args_list]
                self.assertEqual([line for line in sent if line.startswith(b'rcpt TO:')],
                                 [b'rcpt TO:<one@example.com>\r\n', b'rcpt TO:<two@example.com>\r\n'])
                self.assertEqual(sum(line.endswith(b'\r\n.\r\n') for line in sent), int(ready in replies))

    def test_upload_disconnect_records_underlying_timeout(self):
        client = _SubmissionSMTP()
        client.ehlo_resp = b'OK'
        client.sock = Mock()
        client.file = BytesIO(b'250 sender\r\n250 first\r\n250 second\r\n354 upload\r\n')

        def upload(data):
            if data.endswith(b'\r\n.\r\n'):
                raise TimeoutError('sensitive socket payload')

        client.sock.sendall.side_effect = upload
        config = dict(self.config, smtp_user='', from_email='sender@example.com', cc_emails='')
        with patch('market_rates.report_email._SubmissionSMTPSSL', return_value=client):
            result = send_mail(config, self.batch, self.confirm)
        self.assertEqual((result['status'], result['phase'], result['transport_error']),
                         ('unknown', 'data_upload', 'TimeoutError'))
        self.assertNotIn('sensitive socket payload', Path(result['record']).read_text())

    @patch('market_rates.report_email._SubmissionSMTPSSL')
    def test_preview_disabled_cancel_and_duplicate(self, smtp):
        client = smtp.return_value
        client.send_message.return_value = {}
        self.assertEqual(send_mail(dict(self.config, enabled=False), self.batch, self.confirm)['status'], 'disabled')
        preview = send_mail(self.config, self.batch, self.confirm, dry_run=True)
        self.assertIn('格式对齐调研.xlsx', preview['preview'])
        self.confirm.assert_not_called()
        smtp.assert_not_called()
        send_mail(self.config, self.batch, self.confirm)
        self.assertFalse(self.confirm.call_args.args[1])
        self.confirm.return_value = False
        self.assertEqual(send_mail(self.config, self.batch, self.confirm)['status'], 'cancelled')
        self.assertTrue(self.confirm.call_args.args[1])
        client.send_message.assert_called_once()

    @patch('market_rates.report_email._SubmissionSMTPSSL')
    def test_concurrent_send_is_blocked(self, smtp):
        folder = self.work / 'email-attempts'
        folder.mkdir()
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(send_mail(self.config, self.batch, self.confirm)['status'], 'busy')
        smtp.assert_not_called()
        self.confirm.assert_not_called()

    @patch('market_rates.report_email._SubmissionSMTPSSL')
    def test_attachment_change_after_confirmation_is_rejected(self, smtp):
        def change_attachment(*_):
            self.batch.files['raw_report'].write_bytes(b'changed')
            return True
        with self.assertRaisesRegex(ValueError, '被修改'):
            send_mail(self.config, self.batch, change_attachment)
        smtp.assert_not_called()

    def test_incomplete_mismatched_and_missing_deliveries(self):
        for fields in ({'status': 'failed'}, {'report': str(self.batch.files['raw_rainbow'])}):
            with self.subTest(fields=fields):
                save(self.manifest, dict(self.state, **fields))
                with self.assertRaises(ValueError):
                    verified_batch(self.work, self.root)
        save(self.manifest, self.state)
        self.batch.files['raw_rainbow'].unlink()
        with self.assertRaisesRegex(ValueError, '缺失'):
            verified_batch(self.work, self.root)

    def test_background_run_skips_without_opening_dialog_or_verifying_batch(self):
        with (patch('scripts.send_report_email.read_config', return_value=self.config),
              patch('scripts.send_report_email.sys.stdin.isatty', return_value=False),
              patch('scripts.send_report_email.verified_batch') as verify,
              redirect_stdout(StringIO()) as out):
            offer_after_completion(self.work)
        self.assertIn('非交互', out.getvalue())
        verify.assert_not_called()

    @patch('market_rates.report_email._submit', return_value={'status': 'accepted', 'smtp_code': 250})
    def test_split_confirms_once_and_keeps_attachments_and_attempts_separate(self, submit):
        result = send_mail(self.config, self.batch, self.confirm, split='both', no_cc=True)
        self.assertEqual(result['status'], 'accepted')
        self.confirm.assert_called_once()
        preview, _ = self.confirm.call_args.args
        self.assertIn('全量报表（1/2）', preview)
        self.assertIn('格式对齐报表（2/2）', preview)
        self.assertEqual(preview.count('附件合计：'), 2)
        ids = set()
        for call, part, keys in zip(submit.call_args_list, result['parts'],
                                    (('raw_report', 'raw_rainbow'), ('formatted_report', 'formatted_rainbow'))):
            _, message, _, recipients = call.args
            self.assertEqual(recipients, ['one@example.com', 'two@example.com'])
            self.assertNotIn('Cc', message)
            self.assertEqual([(p.get_filename(), p.get_payload(decode=True)) for p in message.iter_attachments()],
                             [(self.batch.files[key].name, key.encode()) for key in keys])
            record = load(part['record'])
            self.assertEqual(record['attachment_hashes'], {key: self.batch.hashes[key] for key in keys})
            ids.add(record['message_id'])
        self.assertEqual(len(ids), 2)

    @patch('market_rates.report_email._submit')
    def test_split_rejects_exact_size_limit_before_confirmation_or_send(self, submit):
        attachment = self.batch.files['formatted_report']
        with attachment.open('wb') as out:
            out.truncate(10_000_000 - self.batch.files['formatted_rainbow'].stat().st_size)
        with self.assertRaisesRegex(ValueError, '格式对齐报表.*10,000,000 字节'):
            send_mail(self.config, self.batch, self.confirm, split='both')
        self.confirm.assert_not_called()
        submit.assert_not_called()

    @patch('market_rates.report_email._submit')
    def test_split_retries_only_definite_failure(self, submit):
        submit.side_effect = [{'status': 'failed'}, {'status': 'accepted'}, {'status': 'accepted'}]
        first = send_mail(self.config, self.batch, self.confirm, split='both', no_cc=True)
        self.assertEqual([p['status'] for p in first['parts']], ['failed', 'accepted'])
        self.assertEqual(first['status'], 'partial')
        # A template edit does not change which recipients already got these files.
        revised = dict(self.config, subject_template='Revised subject')
        second = send_mail(revised, self.batch, self.confirm, split='both', no_cc=True)
        self.assertEqual([p['status'] for p in second['parts']], ['accepted', 'already_accepted'])
        self.assertEqual(submit.call_count, 3)
        self.assertIn('全量报表', submit.call_args.args[1]['Subject'])

    @patch('market_rates.report_email._submit')
    def test_split_uncertain_part_requires_targeted_confirmation(self, submit):
        submit.side_effect = [{'status': 'unknown'}, {'status': 'partial'}, {'status': 'accepted'}]
        first = send_mail(self.config, self.batch, self.confirm, split='both', no_cc=True)
        self.assertEqual(first['status'], 'partial')
        self.assertIn('--split raw', show_result(first))
        self.confirm.reset_mock()
        second = send_mail(self.config, self.batch, self.confirm, split='both', no_cc=True)
        self.assertEqual([p['status'] for p in second['parts']], ['needs_review', 'needs_review'])
        self.assertEqual(submit.call_count, 2)
        self.confirm.assert_not_called()
        raw = send_mail(self.config, self.batch, self.confirm, split='raw', no_cc=True)
        self.assertEqual(raw['status'], 'accepted')
        self.assertTrue(self.confirm.call_args.args[1])
        self.assertEqual(submit.call_count, 3)

    @patch('market_rates.report_email._submit')
    def test_split_verifies_second_pair_before_first_submission(self, submit):
        output = self.batch.files['formatted_rainbow']

        def change_after_preview(*_):
            output.write_bytes(b'changed')
            return True

        with self.assertRaisesRegex(ValueError, '附件.*被修改'):
            send_mail(self.config, self.batch, change_after_preview, split='both')
        submit.assert_not_called()

    def test_saved_delivery_mode_applies_to_cli_preview_and_can_be_overridden(self):
        config_path = self.root / 'email.local.json'
        old = {key: value for key, value in self.config.items() if key != 'delivery_mode'}
        save(config_path, old)
        self.assertEqual(read_config(config_path)['delivery_mode'], 'single')
        save_config(dict(old, delivery_mode='both'), config_path)
        config = read_config(config_path)
        with (patch('scripts.send_report_email.read_config', return_value=config),
              patch('scripts.send_report_email.verified_batch', return_value=self.batch),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(send_main(['--workflow', str(self.work), '--dry-run']), 0)
        self.assertEqual(output.getvalue(), preview_config(config, self.batch) + '\n')
        self.assertEqual(output.getvalue().count('主题：'), 2)

        override = send_mail(config, self.batch, self.confirm, split='single', dry_run=True)
        self.assertEqual(override['preview'].count('主题：'), 1)
        raw = send_mail(config, self.batch, self.confirm, split='raw', dry_run=True)
        self.assertIn('全量报表（1/2）', raw['preview'])
        self.assertNotIn('格式对齐报表（2/2）', raw['preview'])
        with self.assertRaisesRegex(ValueError, '发送方式'):
            save_config(dict(config, delivery_mode='raw'), config_path)
        self.assertEqual(read_config(config_path)['delivery_mode'], 'both')

    @patch('market_rates.report_email._submit', return_value={'status': 'accepted', 'smtp_code': 250})
    def test_completion_prompt_uses_saved_split_mode(self, submit):
        config = dict(self.config, delivery_mode='both')
        with (patch('scripts.send_report_email.read_config', return_value=config),
              patch('scripts.send_report_email.verified_batch', return_value=self.batch),
              patch('scripts.send_report_email.sys.stdin.isatty', return_value=True),
              patch('scripts.send_report_email.terminal_confirm', self.confirm),
              redirect_stdout(StringIO())):
            offer_after_completion(self.work)
        self.confirm.assert_called_once()
        self.assertEqual([len(list(c.args[1].iter_attachments())) for c in submit.call_args_list], [2, 2])

    def test_batch_picker_lists_only_completed_deliveries_and_searches_dates(self):
        for name, state in (('all-failed', dict(self.state, status='failed')),
                            ('all-incomplete', dict(self.state, results={})),
                            ('all-foreign', dict(self.state, project=str(self.root.parent)))):
            save(self.root / 'runs' / name / 'workflow.json', state)
        invalid = self.root / 'runs/all-malformed/workflow.json'
        invalid.parent.mkdir()
        invalid.write_text('{', encoding='utf8')

        choices = batch_choices(self.root)
        self.assertEqual(list(choices.values()), [self.work.resolve()])
        for query in ('20261106', '2026/11/06', '2026-11-06', 'ALL-te', '20261106 重建'):
            self.assertEqual(filter_batches(choices, query), tuple(choices))
        self.assertEqual(filter_batches(choices, '20261107'), ())
        self.batch.files['raw_rainbow'].unlink()
        self.assertFalse(batch_choices(self.root))

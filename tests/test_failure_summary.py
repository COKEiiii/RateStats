from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone, timedelta
from io import StringIO
import os
from pathlib import Path
from subprocess import CompletedProcess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.common import load, save
from market_rates.failure_summary import StepFailure, format_summary, run_step
from scripts import run_all, run_macos

ROOT = Path(__file__).resolve().parents[1]


class FailureChannelTests(TestCase):
    def test_sgd_entry_points_load_the_shared_runner(self):
        for module, options in (
            ('run_next_bank_pilot', []),
            ('run_expansion_bank_pilot', []),
            ('run_remaining_bank_pilot', ['--bank', 'BEA']),
        ):
            with self.subTest(module=module), redirect_stdout(StringIO()) as stdout:
                self.assertEqual(run_step(
                    [sys.executable, '-m', 'scripts.'+module, *options, '--help'],
                    cwd=ROOT, env=os.environ, name=module), 0)
                self.assertIn('usage:', stdout.getvalue())

    def execute(self, source, **kwargs):
        with redirect_stdout(StringIO()):
            return run_step([sys.executable, '-c', source], cwd=ROOT, env=os.environ,
                            name='测试子步骤', **kwargs)

    def test_forward_nested_failures_for_only_the_affected_banks(self):
        child = """from market_rates.failure_summary import record_failure
record_failure('无法读取表头位置（HKD 表）', bank='DBS', step='采集', business='外币定存挂牌')
record_failure('无法读取表头位置（HKD 表）', bank='DBS', step='采集', business='外币定存挂牌')
record_failure('官网连接超时', bank='UOB', step='采集')
raise ValueError('采集未完成')
"""
        parent = f"""import os, sys
from market_rates.failure_summary import run_step
run_step([sys.executable,'-c',{child!r}],cwd=os.getcwd(),env=os.environ,name='剩余六家挂牌采集')
"""
        with self.assertRaises(StepFailure) as caught:
            self.execute(parent, business='定存挂牌')
        details = run_all.unique_details(caught.exception.details)
        summary = format_summary('全部挂牌', details)
        self.assertIn('全部挂牌 → 剩余六家挂牌采集', summary)
        self.assertIn('DBS｜外币定存挂牌｜采集：无法读取表头位置（HKD 表）', summary)
        self.assertIn('UOB｜定存挂牌｜采集：官网连接超时', summary)
        self.assertEqual(summary.count('DBS'), 1)
        self.assertNotIn('RHB', summary)

    def test_success_and_tolerated_failures_do_not_leak_into_next_command(self):
        with TemporaryDirectory() as directory:
            log = Path(directory)/'run.log'
            old = """from market_rates.failure_summary import record_failure
record_failure('旧问题',bank='DBS',step='采集')
print('ValueError: 旧问题')
"""
            self.assertEqual(self.execute(old, log=log), 0)
            self.assertEqual(self.execute(old+'raise SystemExit(4)', log=log, allowed=None), 4)
            with self.assertRaises(StepFailure) as caught:
                self.execute("raise SystemExit(7)", log=log, step='采集', business='新元定存促销')
            self.assertIn('旧问题', log.read_text())
            summary = format_summary('新元促销', caught.exception.details)
            self.assertIn('银行未确认｜新元定存促销｜采集：子进程退出码 7', summary)
            self.assertNotIn('旧问题', summary)

    def test_fx_fallback_uses_explicit_bank_and_current_reason(self):
        with self.assertRaises(StepFailure) as caught:
            self.execute("raise ValueError('有效期与原文不符')", bank='HSBC', business='外币定存促销',
                         step='核验')
        self.assertIn('HSBC｜外币定存促销｜核验：ValueError: 有效期与原文不符',
                      format_summary('外币促销采集', caught.exception.details))


class FullWorkflowFailureTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.work = self.root/'runs'/'all-demo batch'
        self.work.mkdir(parents=True)
        self.manifest = self.work/'workflow.json'
        self.addCleanup(os.chdir, Path.cwd())
        self.patch(run_all, 'ROOT', new=self.root)
        self.patch(run_all, 'start_model')
        self.patch('market_rates.runtime', 'ensure_configuration')
        self.patch('market_rates.delivery', 'prepare_templates', return_value={})

    def patch(self, target, name, **kwargs):
        mocker = patch.object(target, name, **kwargs) if not isinstance(target, str) else patch(target+'.'+name, **kwargs)
        self.addCleanup(mocker.stop)
        return mocker.start()

    def prepare_resume(self, completed, results=None):
        save(self.manifest, dict(project=str(self.root), status='failed', mode='live',
                                 started_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                                 completed=completed, results=results or {},
                                 seeds={'final_output': 'test-template'},
                                 failure_details=[dict(bank='STALE',reason='previous attempt')]))

    def run_failed(self, completed, *, results=None, child=None):
        self.prepare_resume(completed, results)
        if child:
            def fake_command(command, **kwargs):
                kwargs['cwd'] = ROOT
                source = child(command) if callable(child) else child
                return run_step([sys.executable, '-c', source], **kwargs)
            self.patch(run_all, 'run_step', side_effect=fake_command)
        terminal = StringIO()
        with redirect_stdout(terminal), redirect_stderr(terminal):
            self.assertEqual(run_all.main(['--resume', str(self.work), '--no-open']), 1)
        content = terminal.getvalue()
        final = content[content.rindex('全量任务失败｜'):]
        self.assertLess(content.index('识别修复汇总：'), content.index('全量任务失败｜'))
        self.assertNotIn('Traceback', final)
        self.assertNotIn('STALE', final)
        self.assertTrue(final.rstrip().endswith(f"--resume '{self.work}'"))
        state = load(self.manifest)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['completed'], completed)
        self.assertTrue(state['error'])
        self.assertNotIn('STALE', str(state['failure_details']))
        self.assertIn('Traceback', (self.work/'失败详情.log').read_text())
        return final, state['failure_details']

    def test_sgd_group_reports_only_bank_that_failed(self):
        final, details = self.run_failed(['本机依赖检查'], child="""from market_rates.failure_summary import record_failure
record_failure('促销条款链接无法唯一匹配',bank='HLB',step='采集',substep='OCBC-HLB-SBI采集与验证')
raise ValueError('组内采集失败')
""")
        self.assertIn('HLB｜新元定存促销｜采集：促销条款链接无法唯一匹配', final)
        self.assertEqual([item['bank'] for item in details], ['HLB'])

    def test_fx_verification_failure_uses_the_current_bank(self):
        def child(command):
            if 'scripts.verify_fx_wave' in command:
                return "raise ValueError('期限核验未通过')"
            return "print('采集成功')"
        final, _ = self.run_failed(['本机依赖检查', '新元促销', '全部挂牌'], child=child)
        self.assertIn('BEA｜外币定存促销｜核验：ValueError: 期限核验未通过', final)

    def test_excel_delivery_failure_does_not_invent_a_bank(self):
        fx = self.work/'fx-output'
        save(fx/'table-validation-plan.json', {'report_output': 'report.xlsx', 'rainbow_output': 'rainbow.xlsx'})
        self.patch('market_rates.delivery', 'publish_four', side_effect=ValueError('Excel 原生打开检查未通过'))
        final, details = self.run_failed(
            ['本机依赖检查','新元促销','全部挂牌','外币促销采集','外币促销插表','Premier促销同步','新元增幅检查'],
            results={'fx': {'output': str(fx)}, 'fx_source': str(self.work/'fx-combined')})
        self.assertIn('格式对齐与四份输出', final)
        self.assertIn('Excel 原生打开检查未通过', final)
        self.assertNotIn('银行未确认', final)
        self.assertIsNone(details[0]['bank'])

    def test_successful_resume_prints_delivery_and_clears_failure_details(self):
        fx = self.work/'fx-output'
        save(fx/'table-validation-plan.json', {'report_output': 'report.xlsx', 'rainbow_output': 'rainbow.xlsx'})
        save(self.work/'source'/'run.json', {'as_of': '2026-11-10'})
        results = dict(fx={'output': str(fx)}, fx_source=str(self.work/'source'),
                       sgd={'source_run': str(self.work/'source')}, board={'source_run': str(self.work/'source')})
        self.prepare_resume(['本机依赖检查','新元促销','全部挂牌','外币促销采集',
                             '外币促销插表','Premier促销同步','新元增幅检查'], results)
        files = {key: key+'.xlsx' for key in ('raw_report','raw_rainbow','formatted_report','formatted_rainbow')}
        self.patch('market_rates.delivery', 'publish_four', return_value={'files': files, 'hashes': {}})
        self.patch('market_rates.delivery', 'verify_delivery')
        self.patch('market_rates.delivery_records', 'package_records', return_value={'index': 'record.html'})
        self.patch('scripts.send_report_email', 'offer_after_completion')
        terminal = StringIO()
        with redirect_stdout(terminal), redirect_stderr(terminal):
            self.assertEqual(run_all.main(['--resume', str(self.work), '--no-open']), 0)
        self.assertIn('四份Excel', terminal.getvalue())
        self.assertNotIn('全量任务失败', terminal.getvalue())
        state = load(self.manifest)
        self.assertEqual(state['status'], 'complete')
        self.assertNotIn('failure_details', state)

    def test_macos_returns_child_status_without_a_second_exception(self):
        with (patch.object(sys, 'argv', ['run_macos', 'run']),
              patch.object(run_macos.subprocess, 'run', return_value=CompletedProcess([], 7)) as spawn):
            self.assertEqual(run_macos.main(), 7)
        self.assertFalse(spawn.call_args.kwargs.get('check', False))

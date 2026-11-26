import os,sys,unittest
from copy import deepcopy
from subprocess import CompletedProcess
from types import SimpleNamespace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from scripts import run_all,run_macos
from scripts.batch_picker import filter_batches
from scripts.resume_gui import display_start,picker_action,saved_batches
from scripts.run_all import project_lock,preflight,resume_strategy,start_model,seeds
from scripts.run_board_workflow import board_inputs
from market_rates.common import save

class UnifiedLocalTests(unittest.TestCase):
    def test_migrated_windows_output_pointers_do_not_seed_a_mac_run(self):
        with TemporaryDirectory() as temp,patch('scripts.run_all.ROOT',Path(temp)):
            root=Path(temp)
            for name in ('workflow','board-pilot','fx-promo'):
                save(root/'outputs'/('latest-'+name+'.json'),{'output':r'D:\automation\llm-auto\outputs\old',
                    'source_run':r'D:\automation\llm-auto\runs\old'})
            self.assertEqual(seeds(),{})
    def test_first_board_run_uses_explicit_inputs_without_previous_pointer(self):
        with TemporaryDirectory() as temp,patch('scripts.run_board_workflow.ROOT',Path(temp)):
            root=Path(temp);base=root/'base';save(base/'table-validation-plan.json',{'marker':'selected'})
            self.assertEqual(board_inputs(root/'source',base),(root/'source',{'marker':'selected'}))
            pointer=root/'outputs/latest-board-pilot.json';pointer.parent.mkdir();pointer.write_text('broken old pointer')
            self.assertEqual(board_inputs(root/'source',base)[1],{'marker':'selected'})

    def test_board_inputs_can_reuse_valid_previous_output(self):
        with TemporaryDirectory() as temp,patch('scripts.run_board_workflow.ROOT',Path(temp)):
            root=Path(temp);save(root/'base/table-validation-plan.json',{'marker':'previous'})
            save(root/'outputs/latest-board-pilot.json',{'source_run':str(root/'source'),'output':str(root/'base')})
            self.assertEqual(board_inputs(),(root/'source',{'marker':'previous'}))

    def test_first_board_run_reports_missing_inputs_clearly(self):
        with TemporaryDirectory() as temp,patch('scripts.run_board_workflow.ROOT',Path(temp)):
            with self.assertRaisesRegex(ValueError,'首次运行挂牌'):board_inputs()

    def test_model_start_uses_file_log_and_passes_child_environment(self):
        with TemporaryDirectory() as temp,patch('scripts.run_all.start_local_model',return_value={'status':'reused'}) as process:
            start_model(temp,{'RATE_OLLAMA_URL':'http://127.0.0.1:11436'})
            options=process.call_args.kwargs
            self.assertEqual(options['env']['RATE_OLLAMA_URL'],'http://127.0.0.1:11436')
            self.assertEqual(Path(process.call_args.args[0]),Path(temp)/'00模型启动.log')

    def test_model_start_failure_blocks_preflight(self):
        with TemporaryDirectory() as temp,patch('scripts.run_all.start_local_model',side_effect=OSError('offline')):
            with self.assertRaisesRegex(RuntimeError,'模型启动失败'):start_model(temp)

    def state(self):
        return dict(mode='live',status='failed',started_at='2026-10-08T09:00:00',results={})

    def test_same_day_failed_live_resumes_without_changing_state(self):
        state=self.state();before=deepcopy(state)
        self.assertEqual(resume_strategy(state,date(2026,10,8))[0],'resume')
        self.assertEqual(state,before)

    def test_previous_day_live_starts_new_run_and_keeps_old_state(self):
        state=self.state();before=deepcopy(state)
        action,message=resume_strategy(state,date(2026,10,9))
        self.assertEqual(action,'fresh_live');self.assertIn('避免混合采集日期',message)
        self.assertEqual(state,before)

    def test_unknown_start_date_cannot_resume_live_into_mixed_dates(self):
        state=self.state();state.pop('started_at')
        self.assertEqual(resume_strategy(state,date(2026,10,8))[0],'fresh_live')

    def test_legacy_complete_live_restarts_but_new_delivery_is_only_verified(self):
        state=self.state();state['status']='complete'
        self.assertEqual(resume_strategy(state,date(2026,10,8))[0],'fresh_live')
        state['results']['delivery']={'status':'complete'}
        self.assertEqual(resume_strategy(state,date(2026,10,20))[0],'complete')

    def test_historical_rebuild_never_starts_live_collection(self):
        state=self.state();state['mode']='rebuild'
        self.assertEqual(resume_strategy(state,date(2026,10,20))[0],'resume')
        state['status']='complete'
        self.assertEqual(resume_strategy(state,date(2026,10,20))[0],'complete')

    def test_aware_start_date_is_compared_in_singapore_timezone(self):
        state=self.state();state['started_at']='2026-10-07T17:00:00+00:00'
        self.assertEqual(resume_strategy(state,date(2026,10,8))[0],'resume')
    @unittest.skipUnless(os.name=='posix','POSIX local runner')
    def test_posix_lock_rejects_second_runner_and_releases_after_error(self):
        with TemporaryDirectory() as temp,patch('scripts.run_all.ROOT',Path(temp)):
            with self.assertRaisesRegex(ValueError,'stop'):
                with project_lock():
                    with self.assertRaisesRegex(RuntimeError,'已有全量任务'):
                        with project_lock():pass
                    raise ValueError('stop')
            with project_lock():pass
    def test_picker_searches_launch_time_and_uses_existing_resume_policy(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            today=date(2026,10,9)
            current=dict(self.state(), project=str(root), started_at='2026-10-08T17:02:03+00:00')
            past=dict(current, started_at='2026-10-08T09:00:00+08:00')
            rebuilt=dict(past, mode='rebuild')
            finished=dict(current, status='complete', results={'delivery': {'status': 'complete'}})
            for key, state in [('today',current), ('past',past), ('rebuilt',rebuilt), ('finished',finished)]:
                save(root/'runs'/('all-'+key)/'workflow.json',state)
            choices=saved_batches(root)
            found=filter_batches(choices,'2026/10/09 01:02:03 today')
            self.assertEqual(len(found),1)
            self.assertEqual(display_start(current),'2026-10-09 01:02:03')
            by_name={item.work.name:item for item in choices.values()}
            self.assertEqual(picker_action(by_name['all-today'],root,today=today)[0],'继续此批次')
            self.assertIsNone(picker_action(by_name['all-past'],root,today=today)[0])
            self.assertEqual(picker_action(by_name['all-rebuilt'],root,today=today)[0],'继续此批次')
            self.assertEqual(picker_action(by_name['all-finished'],root,today=today)[0],'查看交付')

    def test_resume_launcher_dispatches_selected_directory_or_cancels(self):
        work=run_macos.ROOT/'runs/all-selected'
        with (patch.object(sys,'argv',['run_macos','resume']),
              patch('scripts.resume_gui.choose_workflow',return_value=work) as choose,
              patch.object(run_macos.subprocess,'run',return_value=CompletedProcess([],7)) as spawn):
            self.assertEqual(run_macos.main(),7)
            choose.assert_called_once_with(run_macos.ROOT)
            self.assertEqual(spawn.call_args.args[0],
                             [sys.executable,'-m','scripts.run_all','--resume',str(work),'--resume-only','--mail-gui'])
            spawn.reset_mock()
            choose.return_value=None
            self.assertEqual(run_macos.main(),0)
            spawn.assert_not_called()

    def test_resume_only_does_not_start_fresh_if_batch_became_ineligible(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            work=root/'runs/all-past'
            original=dict(self.state(), project=str(root))
            save(work/'workflow.json',original)
            args=SimpleNamespace(check=False,resume=str(work),resume_only=True)
            with (patch.object(run_all,'ROOT',root),patch.object(run_all.os,'chdir'),
                  patch('market_rates.runtime.ensure_configuration'),
                  patch.object(run_all,'resume_strategy',return_value=('fresh_live','old')),
                  patch.object(run_all,'seeds') as seeds):
                with self.assertRaisesRegex(ValueError,'无法按原断点续跑'):
                    run_all.run(args)
            seeds.assert_not_called()
            self.assertEqual(list((root/'runs').iterdir()),[work])
            self.assertEqual(run_all.load(work/'workflow.json'),original)

    def test_cloud_model_configuration_rejected_before_launch(self):
        with TemporaryDirectory() as temp,patch('scripts.run_all.ROOT',Path(temp)),patch('scripts.run_all.runtimes',return_value={}):
            config=Path(temp)/'config';config.mkdir()
            save(config/'project.local.json',dict(models=dict(provider='ollama',base_url='https://example.com')))
            with self.assertRaisesRegex(ValueError,'只允许本机Ollama'):preflight()

"""One local entry: fresh rates -> Premier sync -> four validated workbooks."""
import argparse,contextlib,json,os,sys,traceback
from datetime import datetime,timezone,timedelta
from pathlib import Path
from urllib.parse import urlsplit
from market_rates.common import load,save
from market_rates.table_validation import sha
from market_rates.failure_summary import (
    StepFailure, WorkflowFailure, detail, format_summary, run_step, unique_details,
)
from scripts.run_board_workflow import runtimes
from scripts.platform_runtime import open_path,prepare_environment,start_local_model,available_output

ROOT=Path(__file__).resolve().parents[1]
FX_BANKS=['BEA','BOC','CIMB','CITI','DBS','HSBC','ICBC','RHB','SBI','SCB']

def start_model(work,env=None):
    """Start or reuse the configured local Ollama service."""
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    log_path=work/'00模型启动.log'
    try:
        result=start_local_model(log_path,env=prepare_environment(env))
        with log_path.open('a',encoding='utf8') as log:log.write(json.dumps(result,ensure_ascii=False)+'\n')
    except Exception as exc:
        raise RuntimeError('本机模型启动失败，详情：'+str(log_path)) from exc

@contextlib.contextmanager
def project_lock():
    path=ROOT/'data/unified-run.lock';path.parent.mkdir(exist_ok=True)
    with path.open('a+b') as f:
        import fcntl
        try:fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('已有全量任务正在运行，请使用已有窗口查看进度。')
        try:yield
        finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def preflight():
    from market_rates.model_adapter import provider
    from market_rates.ollama_adapter import settings
    cfg=load(ROOT/'config/project.local.json')
    try:provider(cfg);settings(cfg)
    except ValueError as exc:raise ValueError('只允许本机Ollama：'+str(exc)) from exc
    from scripts.check_environment import check
    result=check(browser=True,excel=True,model=True)
    if not result['passed']:raise ValueError('运行环境检查未通过：'+'；'.join(c['name']+'：'+c.get('error','') for c in result['checks'] if not c['passed']))
    return result

def seeds():
    """Read this project's existing outputs when present; first run starts empty."""
    result={}
    for name in ('workflow','board-pilot','fx-promo'):
        path=ROOT/'outputs'/('latest-'+name+'.json')
        if not path.exists():continue
        pointer=load(path)
        output=available_output(pointer.get('output'),root=ROOT)
        if output is None:continue
        pointer=dict(pointer,output=str(output))
        if name=='workflow':
            result['sgd_output']=pointer['output']
            result['sgd_source']=load(Path(pointer['output'])/'table-validation-plan.json')['source_run']
        elif name=='board-pilot':result.update(board_source=pointer['source_run'],board_output=pointer['output'])
        else:result.update(fx_source=pointer['source_run'],final_output=pointer['output'])
    return result

def resume_strategy(state,today=None):
    """Choose a safe entry path without mutating the saved historical run."""
    if state.get('status')=='complete':
        if state.get('mode')=='live' and not state.get('results',{}).get('delivery'):
            return 'fresh_live','旧版任务未包含 Premier 同步和四份交付，将保留旧任务并按当前输入设置新建完整实时采集。'
        return 'complete',''
    if state.get('mode')=='live':
        zone=timezone(timedelta(hours=8))
        today=today or datetime.now(zone).date()
        try:
            started=datetime.fromisoformat(state.get('started_at',''))
            started_day=(started.astimezone(zone) if started.tzinfo else started).date()
        except (TypeError,ValueError):
            started_day=None
        if started_day!=today:
            return 'fresh_live','原任务并非今天启动；为避免混合采集日期，将保留旧进度并按当前输入设置新建当日完整实时采集。'
    # Rebuild uses frozen historical evidence, even when the task is old.
    return 'resume',''


def run(args):
    os.chdir(ROOT)
    from market_rates.runtime import ensure_configuration
    ensure_configuration()
    with project_lock():
        if args.check:
            start_model(ROOT/'data')
            result=preflight();save(ROOT/'data/standalone-check.json',result);print('本机依赖检查通过：浏览器、Excel、Python和本地文字模型均可用。');return
        if args.resume:
            work=Path(args.resume).resolve();state=load(work/'workflow.json')
            if state['project']!=str(ROOT):raise ValueError('不是本项目的工作流')
            args.mode=state['mode']
            action,message=resume_strategy(state)
            if action=='complete':
                if state.get('results',{}).get('delivery'):
                    from market_rates.delivery import verify_delivery
                    verify_delivery(state['output'])
                    from market_rates.delivery_records import package_records
                    package_records(work,state=state)
                print('任务已经完成：'+state['output'])
                if not args.no_open:open_path(state.get('formatted_report',state['report']))
                return
            if action=='fresh_live':
                if getattr(args, 'resume_only', False):
                    raise ValueError('此批次无法按原断点续跑，请重新选择批次或使用「一键运行」。')
                print(message+'\n旧任务：'+str(work),flush=True)
                # Fall through to the existing fresh path under the same lock.
                # Never edit the old manifest or recursively acquire its lock.
                args.resume=None;args.mode='live'
        if not args.resume:
            work=ROOT/'runs'/('all-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));work.mkdir()
            selected=seeds()
            from market_rates.input_workbooks import configuration,prepare_selected_inputs
            inputs=configuration()
            if inputs['mode']=='selected' or not selected.get('final_output'):
                if inputs['mode']!='selected':inputs=dict(mode='selected',report=str(ROOT/'inputs/baseline-report.xlsx'),rainbow=str(ROOT/'inputs/baseline-rainbow.xlsx'))
                selected['final_output']=prepare_selected_inputs(work,inputs)
            if args.mode=='rebuild' and any(not selected.get(k) for k in ('sgd_source','board_source','fx_source')):
                raise ValueError('本项目尚无完整采集记录，不能重建；请先运行实时采集。')
            state=dict(project=str(ROOT),mode=args.mode,status='running',completed=[],seeds=selected,input_selection=inputs,results={},started_at=datetime.now(timezone(timedelta(hours=8))).isoformat())
        from market_rates.delivery import prepare_templates
        state.pop('failure_details', None)
        state.pop('error', None)
        state.update(status='running', stage='准备交付样板')
        manifest=work/'workflow.json';save(manifest,state)
        save(ROOT/'outputs/latest-all-run.json',dict(workflow=str(work),status=state['status']))
        env=prepare_environment(os.environ)
        seed=state['seeds'];results=state['results']
        def command(key,module,*argv,bank=None,business=None,step=None):
            cmd=[sys.executable,'-X','utf8','-m',module,*map(str,argv)]
            print('\n'+key,flush=True)
            run_step(cmd,cwd=ROOT,env=env,name=key,log=work/(key+'.log'),
                     bank=bank,business=business,step=step)
        def stage(key,fn):
            if key in state['completed']:return
            state.update(stage=key,status='running');state.pop('error',None);save(manifest,state)
            fn();state['completed'].append(key);save(manifest,state)
        try:
            state['format_templates']=prepare_templates(work)
            state['stage']='本机模型启动'
            start_model(work,env)
            stage('本机依赖检查',lambda:save(work/'preflight.json',preflight()))
            def sgd():
                folder=work/'sgd';receipt=work/'sgd-result.json'
                argv=['--mode',args.mode,'--no-open','--receipt',receipt]
                if (folder/'workflow.json').exists():argv+=['--resume',folder]
                else:
                    argv+=['--work-dir',folder,'--template-output',seed['final_output'],'--template-current']
                    if args.mode=='rebuild':argv+=['--source-run',seed['sgd_source']]
                command('01新元促销','scripts.run_market_workflow',*argv,business='新元定存促销')
                results['sgd']=load(receipt)
            stage('新元促销',sgd)
            def board():
                if args.mode=='rebuild':
                    argv=['--resume',results['board_attempt']] if results.get('board_attempt') else ['--run',seed['board_source'],'--base-output',results['sgd']['output']]
                    try:command('02全部挂牌','scripts.run_board_wide',*argv,'--no-open',business='定存挂牌')
                    except Exception:
                        error=ROOT/'data/board-wide-last-error.json'
                        if error.exists():
                            failed=load(error);attempt=load(Path(failed['output'])/'table-validation-plan.json')
                            if Path(failed['source_run']).resolve()==Path(seed['board_source']).resolve() and Path(attempt.get('base_output','')).resolve()==Path(results['sgd']['output']).resolve():results['board_attempt']=failed['output']
                        raise
                    result=load(ROOT/'outputs/latest-board-pilot.json')
                    results.pop('board_attempt',None)
                else:
                    folder=work/'board';argv=['--no-open']
                    argv+=['--resume',folder] if (folder/'workflow.json').exists() else ['--work-dir',folder,'--base-output',results['sgd']['output']]
                    command('02全部挂牌','scripts.run_board_bulk',*argv,business='定存挂牌');result=load(folder/'workflow.json')
                results['board']={k:result[k] for k in ['output','source_run']}
            stage('全部挂牌',board)
            def capture_fx():
                root=work/'fx-capture'
                for bank in FX_BANKS:
                    folder=root/bank
                    command('03外币采集-'+bank,'scripts.capture_fx_promo','--out',root,'--banks',bank,bank=bank,business='外币定存促销',step='采集')
                    command('03外币核验-'+bank,'scripts.verify_fx_wave','--run',folder,bank=bank,business='外币定存促销',step='核验')
                combined=work/'fx-combined'
                if combined.exists():
                    combined.rename(work/('fx-combined-attempt-'+datetime.now().strftime('%H%M%S%f')))
                command('03外币汇总','scripts.prepare_fx_run','--waves',*[root/b for b in FX_BANKS],'--out',combined,'--board',results['board']['source_run'],business='外币定存促销',step='汇总')
                results['fx_source']=str(combined)
            if args.mode=='live':stage('外币促销采集',capture_fx)
            else:results['fx_source']=seed['fx_source']
            def fx():
                command('04外币促销插表','scripts.run_fx_promo','--run',results['fx_source'],'--base-output',results['board']['output'],'--no-open',business='外币定存促销',step='Excel 插表')
                results['fx']=load(ROOT/'outputs/latest-fx-promo.json')
            stage('外币促销插表',fx)
            p=load(Path(results['fx']['output'])/'table-validation-plan.json')
            def premier():
                from market_rates.premier_sync import sync_premier
                results['premier']=sync_premier(p['report_output'],results['sgd']['source_run'],results['fx_source'],audit_path=work/'premier-sync.json')
            stage('Premier促销同步',premier)
            from market_rates.sgd_comparison import check_saved
            stage('新元增幅检查',lambda:save(work/'sgd-comparison.json',check_saved(p['report_output'])))
            def delivery():
                from market_rates.delivery import publish_four
                results['delivery']=publish_four(p['report_output'],p['rainbow_output'],state['format_templates'],
                    ROOT/'outputs'/work.name,project_root=ROOT,started_at=state['started_at'])
            stage('格式对齐与四份输出',delivery)
            from market_rates.delivery import verify_delivery
            verify_delivery(ROOT/'outputs'/work.name)
            source_dates={key:load(Path(results[key]['source_run'])/'run.json')['as_of'] for key in ['sgd','board']}
            source_dates['fx']=load(Path(results['fx_source'])/'run.json')['as_of']
            files=results['delivery']['files']
            state.update(status='complete',finished_at=datetime.now().isoformat(),output=str(ROOT/'outputs'/work.name),source_dates=source_dates,
                         report=files['raw_report'],rainbow=files['raw_rainbow'],
                         formatted_report=files['formatted_report'],formatted_rainbow=files['formatted_rainbow'],
                         output_hashes=results['delivery']['hashes'])
            state.pop('error',None)
            from market_rates.repair_feedback import write_summary
            from market_rates.delivery_records import package_records
            write_summary(work)
            record_package=package_records(work,state=state)
            save(manifest,state);save(ROOT/'outputs/latest-all.json',dict(workflow=str(work),output=state['output'],mode=args.mode,source_dates=source_dates))
            save(ROOT/'outputs/latest-all-run.json',dict(workflow=str(work),status='complete'))
            print('\n全量采集、Premier同步和格式对齐完成，四份Excel：'+state['output'],flush=True)
            for path in files.values():print(path,flush=True)
            print('运行记录：'+record_package['index'],flush=True)
            print('各部分资料日期：'+json.dumps(source_dates,ensure_ascii=False)+'；重建不会伪装成重新采集。',flush=True)
            if not args.no_open:open_path(files['formatted_report'])
        except Exception as exc:
            log=work/'失败详情.log'
            with log.open('a',encoding='utf8') as stream:
                traceback.print_exc(file=stream)
            failed_stage=state['stage']
            details=unique_details(exc.details) if isinstance(exc,StepFailure) else [
                detail(str(exc) or type(exc).__name__,step=failed_stage,log=log)]
            state.update(status='failed',error=str(exc),failure_details=details);save(manifest,state)
            save(ROOT/'outputs/latest-all-run.json',dict(workflow=str(work),status='failed'))
            summary_log=(exc.log or log) if isinstance(exc,StepFailure) else log
            raise WorkflowFailure(work,failed_stage,details,summary_log) from exc
        finally:
            try:
                from market_rates.repair_feedback import write_summary
                print('识别修复汇总：'+str(write_summary(work)),flush=True)
            except Exception as feedback_error:
                print('识别汇总未生成；原始银行日志仍保留：'+str(feedback_error),file=sys.stderr)

    # The delivery is already committed. Email has a separate result and retry path.
    try:
        from scripts.send_report_email import offer_after_completion
        offer_after_completion(work, gui=getattr(args, 'mail_gui', False))
    except (Exception, KeyboardInterrupt) as exc:
        from scripts.send_report_email import error_text
        print('邮件未完成：'+error_text(exc)+' 请使用“发送结果邮件.command”。',file=sys.stderr)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['live','rebuild'],default='live')
    parser.add_argument('--resume')
    parser.add_argument('--resume-only', action='store_true', help='只继续指定批次；需要新采集时停止')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--mail-gui',action='store_true')
    args=parser.parse_args(argv)
    if args.resume_only and (not args.resume or args.check):
        parser.error('--resume-only 必须与 --resume 一起使用')
    try:
        run(args)
    except WorkflowFailure as exc:
        print(format_summary(exc.stage,exc.details,exc.log,exc.work),file=sys.stderr,flush=True)
        return 1
    except Exception as exc:
        stage='本机依赖检查' if args.check else '读取续跑批次' if args.resume else '初始化任务'
        log=ROOT/'data/全量启动失败.log'
        try:
            log.parent.mkdir(exist_ok=True)
            with log.open('a',encoding='utf8') as stream:
                traceback.print_exc(file=stream)
        except OSError:
            log=None
        print(format_summary(stage,[detail(str(exc) or type(exc).__name__)],log),file=sys.stderr,flush=True)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())

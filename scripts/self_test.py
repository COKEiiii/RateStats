"""Portable native Excel regression. Synthetic historical input, never live quotes."""
import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
from market_rates.common import save,load
from market_rates.runtime import ROOT,ensure_configuration
from market_rates.table_validation import sha
from market_rates.input_sheets import create
from market_rates.delivery import prepare_templates,publish_four,verify_delivery
from market_rates.multi_bank_validation import plan_multi
from market_rates.board_wide_plan import plan as board_plan
from market_rates.fx_promo_plan import plan as fx_plan
from scripts.inspect_weekly_templates import inspect
from tests.planning_fixture import prepare
from scripts.platform_runtime import excel_command,prepare_environment


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    out=Path(args.out).resolve() if args.out else ROOT/'data'/('self-test-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True,exist_ok=args.resume);ensure_configuration()
    state=dict(status='running',synthetic=True,live_collection_tested=False,project=str(ROOT),stages=[])
    if args.resume:
        state=load(out/'self-test.json')
        if state.get('synthetic') is not True:raise ValueError('Only synthetic self-tests can resume')
        state.update(status='running');state.pop('error',None)
    save(out/'self-test.json',state)
    assets=[p for d in ('templates','inputs') for p in (ROOT/d).glob('*.xlsx')]
    hashes={str(p):sha(p) for p in assets}
    def call(*args):
        subprocess.run([str(a) for a in args],cwd=ROOT,check=True,env=prepare_environment())
    def stage(name):
        state['current_stage']=name;save(out/'self-test.json',state)
    def done(stage):
        if stage not in state['stages']:state['stages'].append(stage)
        save(out/'self-test.json',state);print(stage+' passed',flush=True)
    try:
        report=ROOT/'inputs/baseline-report.xlsx';rainbow=ROOT/'inputs/baseline-rainbow.xlsx'
        if sys.platform=='darwin':
            capability_out=out/'native-capabilities';receipt=capability_out/'checks.json'
            completed_capabilities=(args.resume and receipt.is_file() and load(receipt).get('passed')
                                    and sha(load(receipt)['output'])==load(receipt).get('sha256'))
            if not completed_capabilities:
                stage('excel-capabilities')
                if capability_out.exists():capability_out.rename(out/('native-capabilities-failed-'+datetime.now().strftime('%H%M%S%f')))
                call(sys.executable,'-m','scripts.check_macos_excel_capabilities','--out',capability_out)
        if not args.resume:
            for kind in ('sgd','board','fx'):prepare(out/'sources'/kind,kind)
            inspect(report,rainbow,out/'inspection.json')
        if len(state['stages'])<1:
            stage('sgd')
            plan_path=out/'sgd/table-validation-plan.json'
            expected_previews=[out/'sgd'/name for name in (
                'rainbow-1m.pdf','rainbow-18m.pdf','rainbow-24m.pdf',
                'quote-maybank-6m.pdf','quote-maybank-9m.pdf','quote-maybank-12m.pdf')]
            completed_outputs=(args.resume and plan_path.is_file()
                and all(Path(load(plan_path)[kind+'_output']).is_file() for kind in ('report','rainbow'))
                and (out/'sgd/table-validation-checks.json').is_file()
                and all(path.is_file() for path in expected_previews))
            if not completed_outputs:
                sgd=plan_multi(out/'sources/sgd',report,rainbow,out/'inspection.json',out/'sgd')
                create(out/'sgd','validation')
                call(*excel_command('sgd','-PlanPath',plan_path))
                call(sys.executable,'-m','scripts.check_three_bank_validation','--plan',plan_path)
                call(*excel_command('weekly-preview','-PlanPath',plan_path))
            else:
                sgd=load(plan_path)
                call(sys.executable,'-m','scripts.check_three_bank_validation','--plan',plan_path)
            call(*excel_command('style-check','-PlanPath',plan_path))
            done('SGD：明细、调研表、汇总、彩虹表及保存回读')
        else:sgd=load(out/'sgd/table-validation-plan.json')
        if len(state['stages'])<2:
            stage('board')
            board_path=out/'board/table-validation-plan.json'
            completed_board=(args.resume and board_path.is_file()
                and all(Path(load(board_path)[kind+'_output']).is_file() for kind in ('report','rainbow'))
                and (out/'board/native-checks.json').is_file())
            if completed_board:
                board=load(board_path)
            else:
                board=(load(board_path) if board_path.is_file()
                       else board_plan(out/'sources/board',out/'board',sgd))
                if not (out/'board/board-inputs.xlsx').is_file():create(out/'board','board')
                call(*excel_command('board','-PlanPath',board_path))
            call(sys.executable,'-m','scripts.check_board_wide','--output',out/'board')
            done('挂牌：全部币种、金额档位、期限及保存回读')
        else:board=load(out/'board/table-validation-plan.json')
        stage('fx')
        fx=load(out/'fx/table-validation-plan.json') if (out/'fx/table-validation-plan.json').exists() else fx_plan(out/'sources/fx',out/'fx',board)
        create(out/'fx','fx')
        completed_fx=(args.resume and (out/'fx/native-checks.json').is_file()
                      and all(Path(fx[kind+'_output']).is_file() for kind in ('report','rainbow')))
        if not completed_fx:
            command=excel_command('fx','-PlanPath',out/'fx/table-validation-plan.json')
            if args.resume and sys.platform=='darwin':command.append('-Retry')
            call(*command)
        call(sys.executable,'-m','scripts.check_fx_promo','--out',out/'fx')
        done('外币促销：报价、DBS加点公式及保存回读')
        stage('premier')
        from market_rates.premier_sync import sync_premier
        premier_receipt=out/'premier-sync.json'
        if not (args.resume and premier_receipt.exists()
                and load(premier_receipt).get('output_sha256')==sha(fx['report_output'])):
            sync_premier(fx['report_output'],out/'sources/sgd',out/'sources/fx',audit_path=premier_receipt)
        done('Premier 同步')
        stage('delivery')
        templates=prepare_templates(out)
        result=publish_four(fx['report_output'],fx['rainbow_output'],templates,out/'delivery')
        verify_delivery(out/'delivery')
        stage('premier-linkage')
        from market_rates.report_xml import Book
        from market_rates.premier_details import validate_links,DETAIL_SHEET
        manifest={'files':[]}
        for kind in ('raw_report','formatted_report'):
            path=result['files'][kind];book=Book(path);detail=book.sheets[DETAIL_SHEET]
            rows={str(detail.value(row,1)):row for row in range(4,detail.maxrow+1)}
            cases=[]
            for case in validate_links(book):
                row=rows[case['key']]
                cases.append(dict(case,row=row,value=float(detail.value(row,10))))
            manifest['files'].append(dict(kind=kind,path=path,cells=cases))
        manifest_path=out/'premier-native-input.json';save(manifest_path,manifest)
        call(*excel_command('check-premier','--Manifest',manifest_path,'--Out',out/'premier-native-checks.json'))
        done('Premier 原生联动：修改J列、零值、缺失值、排序及编号异常')
        assert hashes=={str(p):sha(p) for p in assets},'Source assets were modified'
        state.update(status='passed',files=result['files'],assets_unchanged=True)
        done('模板格式整理、四份 Excel 原生打开、原始资产未修改')
        state.pop('current_stage',None)
    except Exception as exc:
        state.update(status='failed',error=str(exc));raise
    finally:save(out/'self-test.json',state)
    print('全部测试输出为历史数值测试，不能当作新采集报价。'+str(out),flush=True)


if __name__=='__main__':main()

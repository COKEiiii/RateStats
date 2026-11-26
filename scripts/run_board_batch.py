"""Replay verified local batch into native Excel copies; publish pointer after QA."""
import argparse,os,subprocess,sys,webbrowser
from pathlib import Path
from datetime import datetime
from market_rates.common import load,save
from market_rates.board_batch_plan import plan,ROOT
from scripts.run_board_workflow import runtimes
from scripts.platform_runtime import excel_command,prepare_environment

def main():
    a=argparse.ArgumentParser();a.add_argument('--run');a.add_argument('--base-output');a.add_argument('--no-open',action='store_true');args=a.parse_args();os.chdir(ROOT)
    from scripts.run_board_workflow import board_inputs
    source,base=board_inputs(args.run,args.base_output)
    out=ROOT/'outputs'/('board-batch-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));state=dict(status='running',source_run=str(source.resolve()),output=str(out),completed=[])
    try:
        p=plan(source,out,base);save(out/'workflow.json',state);rt=runtimes()
        for label,cmd in [('报价输入',[rt['validation_python'],'-X','utf8','-m','scripts.build_board_inputs',str(out)]),('Excel副本',excel_command('board-batch','-PlanPath',out/'table-validation-plan.json')),('保存后回读',[rt['validation_python'],'-X','utf8','-m','scripts.check_board_batch','--output',str(out)])]:
            subprocess.run(cmd,env=prepare_environment(),cwd=ROOT,check=True);state['completed'].append(label);save(out/'workflow.json',state)
        state['status']='complete';save(out/'workflow.json',state);save(ROOT/'outputs/latest-board-pilot.json',dict(output=str(out),source_run=str(source.resolve()),human_reviewed=False))
        if not args.no_open:webbrowser.open((out/'table-validation.html').as_uri())
        print('挂牌扩展副本已生成：'+str(out),flush=True)
    except Exception as e:
        state.update(status='failed',error=str(e));save(ROOT/'data/board-batch-last-error.json',state)
        if out.exists():save(out/'workflow.json',state)
        raise
if __name__=='__main__':main()

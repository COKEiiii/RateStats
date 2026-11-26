"""Regenerate this captured FX batch locally, including persistent user corrections."""
import argparse,os,webbrowser
from pathlib import Path
from datetime import datetime
from market_rates.common import load,save
from market_rates.failure_summary import run_step
from market_rates.fx_promo_plan import plan
from market_rates.board_wave import validate_wave
from market_rates.pipeline import check_evidence
from scripts.run_board_workflow import runtimes,ROOT
from scripts.platform_runtime import excel_command,open_path,prepare_environment,available_output
def main():
    a=argparse.ArgumentParser();a.add_argument('--run');a.add_argument('--base-output');a.add_argument('--no-open',action='store_true');x=a.parse_args();os.chdir(ROOT)
    latest=load(ROOT/'outputs/latest-fx-promo.json') if (ROOT/'outputs/latest-fx-promo.json').exists() else {}
    source=Path(x.run or latest['source_run']);run=load(source/'run.json');check_evidence(run,source/'evidence')
    for wave in run['fx_sources']:validate_wave(wave)
    if x.base_output:base=load(Path(x.base_output)/'table-validation-plan.json')
    else:base=load(load(Path(latest['output'])/'table-validation-plan.json')['base_plan'])
    if base.get('kind')!='board-wide':raise ValueError('使用包含本期挂牌和SGD促销的底稿')
    predecessor=available_output(base.get('unified_predecessor') or latest.get('output'),root=ROOT)
    if predecessor:
        from market_rates.history_dates import check_dates
        previous=load(Path(predecessor)/'table-validation-plan.json')
        check_dates(previous['report_output'],base['report_output'])
    out=ROOT/'outputs'/('fx-promo-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));p=plan(source,out,base);rt=runtimes()
    state=dict(status='running',source_run=str(source.resolve()),output=str(out),completed=[]);save(out/'workflow.json',state)
    for label,cmd in [('报价输入',[rt['validation_python'],'-X','utf8','-m','scripts.build_fx_inputs',str(out)]),('Excel插表',excel_command('fx','-PlanPath',out/'table-validation-plan.json')),('保存回读',[rt['validation_python'],'-X','utf8','-m','scripts.check_fx_promo','--out',str(out)])]:
        run_step(cmd,cwd=ROOT,env=prepare_environment(),name=label,step=label);state['completed'].append(label);save(out/'workflow.json',state)
    state['status']='complete';save(out/'workflow.json',state)
    from market_rates.common import offline_replay
    if not offline_replay(run):save(ROOT/'outputs/latest-fx-promo.json',dict(output=str(out),source_run=str(source.resolve()),human_reviewed=False))
    if not x.no_open:open_path(p['report_output'])
    print('外币促销已重新生成；使用本批保存证据，不调用模型API。'+str(out))
if __name__=='__main__':main()

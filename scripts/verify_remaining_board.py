"""Sequential local GPU verification, concise summaries instead of full logs."""
import argparse,sys,os
from scripts.platform_runtime import start_local_model
from pathlib import Path
from market_rates.common import load,save
from market_rates.failure_summary import StepFailure, record_failure, run_step

ROOT=Path(__file__).resolve().parents[1]

def main():
    a=argparse.ArgumentParser();a.add_argument('--run',required=True);a.add_argument('--banks',default='DBS,UOB,RHB,SBI,CIMB,HLB');args=a.parse_args();root=Path(args.run)
    results=[]
    start_local_model(ROOT/'data/local-model/verify-remaining-board.log')
    for bank in args.banks.split(','):
        folder=root/bank
        if not (folder/'run.json').exists():
            record_failure('采集未完成，缺少本批来源记录',bank=bank,step='采集')
            results.append(dict(bank=bank,passed=False,error='采集未完成'));continue
        retry=root/(bank+'-layout-retry')
        if (retry/'run.json').exists():
            folder=retry
        try:
            code=run_step([sys.executable,'-X','utf8','-m','scripts.verify_with_repair','--run',str(folder)],
                          cwd=Path.cwd(),env=os.environ,name='剩余六家本地核验',log=folder/'verification.log',
                          bank=bank,step='核验',stream=False,log_mode='w')
        except StepFailure as exc:
            code=exc.code
        proof=load(folder/'wave-checks.json') if (folder/'wave-checks.json').exists() else dict(passed=False,checks=[],errors=[dict(error='本地核验未完成')])
        feedback=folder/'自动修复结果.txt'
        if feedback.exists():print(feedback.read_text(encoding='utf-8-sig'),flush=True)
        result=dict(bank=bank,path=str(folder.resolve()),passed=proof['passed'] and code==0,checks=len(proof['checks']),failed=len(proof['errors']),cache_hits=sum(bool(c.get('cache_hit')) for c in proof['checks']))
        if code==0 and not proof['passed']:
            record_failure('核验未生成通过记录',bank=bank,step='核验',log=folder/'verification.log')
        results.append(result);save(root/'verification-summary.json',results);print(result,flush=True)
    save(root/'verification-summary.json',results)
    if not all(r['passed'] for r in results):raise ValueError('部分原图转录需修复，查看各银行本地日志')

if __name__=='__main__':main()

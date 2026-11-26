"""Verify currency from captured DOM selection and literal table labels."""
import argparse
from pathlib import Path
from market_rates.common import load,save,digest

def check(root):
    root=Path(root);run=load(root/'run.json');pages={p['id']:p for p in run['pages']};checks=[]
    if digest(run['pages'])!=run['evidence_hash']:raise ValueError('Currency source evidence changed')
    for packet in run['packets']:
        page=pages[packet['pages'][0]];evidence=page.get('currency_evidence',{})
        values=evidence.get('observed_codes',[])
        normalized={'CNY' if c=='RMB' else c for c in values}
        passed=bool(values) and normalized=={packet['currency']}
        checks.append(dict(bank=packet['bank'],currency=packet['currency'],actual=sorted(normalized),passed=passed,source=evidence))
    result=dict(method='Captured selected DOM value and table currency label',passed=bool(checks) and all(c['passed'] for c in checks),checks=checks,evidence_hash=run['evidence_hash'])
    save(root/'currency-checks.json',result)
    if not result['passed']:raise ValueError('币种选择证据缺失或与表格不一致，请重新采集')
    return checks

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);check(p.parse_args().run)

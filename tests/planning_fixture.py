"""Historical values reassembled as explicitly synthetic, self-contained test data."""
from copy import deepcopy
from pathlib import Path
from market_rates.common import save
from market_rates.table_validation import sha
from tests.fixtures import historical,text_run


def prepare(root,kind):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    data=historical(kind+'-planning')
    run=text_run(data['offers'],id='SYNTHETIC-PORTABLE-'+kind,demo=True,
        as_of=data['as_of'],bank_dates=data.get('bank_dates',{}),
        collection_label='SYNTHETIC TEST — 历史数值测试',
        coverage=data.get('coverage',[]),pilot={'no_publication':True},
        wide_sources=[],fx_sources=[],deferred=data.get('deferred',[]),
        source_statuses=data.get('source_statuses',[]))
    for row in run['llm']['offers']:
        run['bank_dates'].setdefault(row['bank'],run['as_of'])
    save(root/'run.json',run)
    save(root/'result.json',dict(pending=1,reason='SYNTHETIC TEST; never a live approval'))
    if kind=='sgd':
        save(root/'config.snapshot.json',data['config'])
        save(root/'benchmark.json',dict(kind='synthetic_test_fixture',
            evidence_hash=run['evidence_hash'],run_sha256=sha(root/'run.json'),
            lanes={'llm':dict(matched=len(data['offers']),expected=len(data['offers']),unexpected=[])}))
    return run

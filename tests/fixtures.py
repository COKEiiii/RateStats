"""Explicit synthetic text runs and small historical parser fixtures; test use only."""
from copy import deepcopy
from pathlib import Path
from market_rates.common import digest,load

ROOT=Path(__file__).resolve().parent/'fixtures'

def text_run(rows,**kwargs):
    rows=deepcopy(rows);pages={}
    for row in rows:
        for e in row['evidence']:
            p=pages.setdefault(e['page_id'],dict(id=e['page_id'],bank=row['bank'],url='https://example.invalid/synthetic-fixture',text='',ok=True,complete=True,images=[],image_hashes={}))
            p['text']+=e['quote']+'\n'
    result=dict(id='synthetic-test',as_of='2026-09-27',pages=list(pages.values()),errors=[],metadata=[],llm=dict(offers=rows,coverage_complete=True,unreadable=[],inventory=[dict(page_id=p,row_count=sum(r['evidence'][0]['page_id']==p for r in rows),notes='synthetic fixture') for p in pages]))
    result.update(kwargs);result['evidence_hash']=digest(result['pages']);return result

def historical(name):return load(ROOT/(name+'.json'))

"""Batch capture bank-specific board sections and literal evidence tasks."""
from market_rates.link_registry import discovery_folder
from market_rates.browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS
import argparse,hashlib,re
from pathlib import Path
from datetime import datetime,timezone,timedelta
from playwright.sync_api import sync_playwright
from market_rates.common import load,save,digest
from market_rates.pipeline import evidence_index

# Only sections explicitly identified as fixed-deposit board rates are enrolled.
# Separate audiences and rollover restrictions remain in the source contract.
SECTIONS=[
 ('DBS-SGD-board','DBS','SGD',0,'dbs-sgd-board'),
 ('HSBC-SGD-board','HSBC','SGD',1,'hsbc-sgd-personal-board'),
 ('HLF-SGD-board','HLF','SGD',0,'hlf-sgd-board'),
 ('Maybank-others-board','Maybank','SGD',3,'maybank-sgd-board'),
 ('Maybank-others-board','Maybank','FX',5,'maybank-fx-tier1-board'),
 ('Maybank-others-board','Maybank','FX',7,'maybank-fx-tier2-board'),
 ('Maybank-others-board','Maybank','FX',9,'maybank-fx-tier3-board'),
 ('OCBC-SGD-board','OCBC','SGD',0,'ocbc-sgd-board'),
 ('SingFinance-SGD-board','SingFinance','SGD',1,'singfinance-sgd-board'),
 ('Singapura Finance-SGD-board','Singapura Finance','SGD',0,'singapura-sgd-board'),
 ('SCB-SGD-board','SCB','SGD',0,'scb-sgd-board'),
 ('SCB-SGD-board','SCB','SGD',1,'scb-sgd-rollover-board'),
 ('SCB-others-board','SCB','FX',-1,'scb-fx-board'),
 ('UOB-SGD-board','UOB','SGD',1,'uob-sgd-board'),
]
TERMS={
 'DBS':['Rates quoted are in % p.a. and are subject to change without prior notice.','Interest rates are indicative. Rates apply to individual accounts only.'],
 'HSBC':['HSBC Personal Banking Interest Rates (p.a)'],
 'HLF':['Minimum placement sum for 1 or 2 months deposit is S$10,000, and for the longer terms is S$500.','For sums of S$1 million and higher, please enquire at our branches for rates.'],
 'Singapura Finance':['$500.00 for 3 months and above $5,000.00 for 1 to 2 months'],
 'OCBC':['*Please note that new placements for tenures of 24-month and longer are not available. Interest rates for tenures of 24-month and longer, apply only to renewal of existing placements at the same tenure.'],
}
PHYSICAL="t=>[...t.rows].map(r=>[...r.cells].map(c=>c.innerText.trim()).filter(Boolean)).filter(r=>r.length)"

def shot(page,locator,path):
    from market_rates.evidence_capture import table_overview
    table_overview(page,locator,path)

def capture(discovery,out,modern_sources=False,ocbc_modern=False,icbc_hlf_modern=False,*,banks=None):
    discovery=Path(discovery);out=Path(out);ev=out/'evidence';ev.mkdir(parents=True,exist_ok=False)
    sections=[];pages=[];tasks=[];errors=[];now=datetime.now(timezone.utc).isoformat()
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True,args=['--disable-http2']);ctx=b.new_context(viewport={'width':1440,'height':1700},device_scale_factor=1.5,locale='en-SG')
        try:
            enrolled=[s for s in SECTIONS if not modern_sources or s[4] not in ['maybank-sgd-board','scb-fx-board']]
            if ocbc_modern:enrolled=[s for s in enrolled if s[1]!='OCBC']
            if icbc_hlf_modern:enrolled=[s for s in enrolled if s[1]!='HLF']
            if banks is not None:enrolled=[s for s in enrolled if s[1] in banks]
            for key in dict.fromkeys(x[0] for x in enrolled):
                src=load(discovery_folder(discovery,key)/'source.json');p=ctx.new_page();bank=src['bank']
                try:
                    resp=p.goto(src['url'],wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
                    if not resp or resp.status!=200:raise ValueError('Bank source unavailable')
                    p.wait_for_timeout(PAGE_SETTLE_MS);(ev/(key+'.html')).write_text(p.content(),encoding='utf8')
                    terms=[]
                    for i,txt in enumerate(TERMS.get(bank,[])):
                        loc=p.get_by_text(txt,exact=True).filter(visible=True).first
                        if loc.count()!=1:raise ValueError('Required condition not found: '+txt)
                        name=key+'-term'+str(i)+'.png';shot(p,loc,ev/name)
                        tid=key+'-term'+str(i);terms.append(tid)
                        tasks.append(dict(id=tid,bank=bank,kind='text',expected=loc.inner_text(),images=[name],page_id=key))
                    images=[];text=p.locator('body').inner_text()
                    for _,bank,cur,idx,pid in [s for s in enrolled if s[0]==key]:
                        indices=range(p.locator('table').count()) if idx==-1 else [idx]
                        for index in indices:
                            table=p.locator('table').nth(index)
                            if not table.is_visible():raise ValueError('Selected table is hidden')
                            rows=table.evaluate(PHYSICAL)
                            # Only physical cells are copied; no duplicate merged headings.
                            original=next(t for t in src['tables'] if t['index']==index)['text']
                            if re.sub(r'\s','',original)!=re.sub(r'\s','',table.inner_text()):raise ValueError('Source changed since discovery; recapture inventory')
                            tid=pid+('-'+str(index) if idx==-1 else '');name=tid+'.png'
                            shot(p,table,ev/name);images.append(name)
                            cap=table.locator('caption');cb=cap.bounding_box() if cap.count() else None
                            caption=cap.inner_text() if cb and cb['width']>2 and cb['height']>2 else ''
                            if caption.strip():rows=[[caption.strip()]]+rows
                            # JPY shares the Maybank PDF/HTML resource, but is not enrolled,
                            # normalized or sent to either inference lane.
                            if cur=='FX' and bank=='Maybank':
                                trs=table.locator('tr');kept=[];parts=[]
                                for ri,tr in enumerate(trs.all()):
                                    cells=tr.locator(':scope > th, :scope > td').all_inner_texts();cells=[v.strip() for v in cells if v.strip()]
                                    if not cells or any('Japanese Yen' in v for v in cells):continue
                                    kept.append(cells)
                                rows=kept;taskimages=[name]
                            else:taskimages=[name]
                            tasks.append(dict(id=tid,bank=bank,kind='grid',expected=rows,images=taskimages,page_id=key))
                            sections.append(dict(id=tid,bank=bank,currency=cur,product_id=pid,table=index,rows=rows,task_ids=[tid]+terms,page_id=key))
                    pages.append(dict(id=key,bank=bank,currency='multi',rate_type='board',url=p.url,text=text,images=images+[n for t in tasks if t['page_id']==key and t['kind']=='text' for n in t['images']],ok=True,complete=True,captured_at=now,sha256=hashlib.sha256(text.encode()).hexdigest()))
                    print(key,'captured',len([x for x in sections if x['page_id']==key]),flush=True)
                except Exception as exc:errors.append(dict(bank=bank,source=key,error=str(exc)));print(key,'ERROR',str(exc)[:160],flush=True)
                finally:p.close()
        finally:b.close()
    for page in pages:page['image_hashes']={n:hashlib.sha256((ev/n).read_bytes()).hexdigest() for n in page['images']}
    for task in tasks:task['image_hashes']={n:hashlib.sha256((ev/n).read_bytes()).hexdigest() for n in task['images']}
    today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    run=dict(id=out.name,as_of=today,bank_dates={p['bank']:datetime.fromisoformat(p['captured_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat() for p in pages},pages=pages,sections=sections,tasks=tasks,errors=errors,evidence_hash=digest(pages),task_hash=digest(tasks),demo=False)
    save(out/'run.json',run);evidence_index(pages,ev);print('Sections',len(sections),'tasks',len(tasks),'errors',errors,flush=True)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--discovery',required=True);a.add_argument('--out',required=True);a.add_argument('--modern-sources',action='store_true');a.add_argument('--ocbc-modern',action='store_true');a.add_argument('--icbc-hlf-modern',action='store_true');a.add_argument('--banks',nargs='+',choices=sorted({s[1] for s in SECTIONS}));args=a.parse_args();capture(args.discovery,args.out,args.modern_sources,args.ocbc_modern,args.icbc_hlf_modern,banks=args.banks)

"""Capture registered foreign-currency promotions and their eligibility evidence locally."""
from market_rates.runtime import bank_links
from market_rates.failure_summary import record_failure
import argparse,re,hashlib,shutil
from pathlib import Path
from datetime import datetime,timezone
import pymupdf
from market_rates.evidence_capture import pdf_overview
from playwright.sync_api import sync_playwright
from market_rates.board_capture import BoardCapture
from market_rates.browser_wait import CONTENT_TIMEOUT_MS,PAGE_SETTLE_MS,PAGE_LOAD_TIMEOUT_MS
from market_rates.common import load,save,digest
from market_rates.link_registry import rate_url
from scripts.capture_board_wave import PHYSICAL

def capture(out,banks):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    def link(bank,cur='others'):return rate_url(bank,cur,'promo')
    failures=[]
    for bank in banks:
        if (out/bank/'run.json').exists():print(bank,'captured already',flush=True);continue
        if (out/bank).exists():
            (out/bank).rename(out/(bank+'-failed-'+datetime.now().strftime('%H%M%S%f')))
        batch=BoardCapture(out/bank);batch.prefix=out.name+'-'+bank+'-'
        with sync_playwright() as pw:
            b=pw.chromium.launch(headless=True,args=['--disable-http2']);ctx=b.new_context(viewport={'width':1700,'height':1900},device_scale_factor=2,locale='en-SG');ctx.set_default_timeout(CONTENT_TIMEOUT_MS)
            try:
                def open_page(key,url):
                    p,page=batch.open(ctx,bank,key,url);page['rate_type']='promo';return p,page
                def txt(key,pattern):return batch.text(p,page,key,p.get_by_text(re.compile(pattern)).filter(visible=True).last)
                def section(key,rows,ids,extra=None,**kw):
                    batch.sections.append(dict(id=batch.prefix+key,bank=bank,currency=kw.pop('currency','multi'),product_id=bank.lower()+'-fx-'+key,layout='fx-promo-'+key,rows=rows,task_ids=ids,page_id=page['id'],extra_evidence=extra or [],**kw))
                def terms(name,pattern):
                    from urllib.parse import urlsplit
                    refs=p.locator('a[href]').evaluate_all('xs=>xs.map(x=>x.href)')
                    matches=list(dict.fromkeys(u.replace('http:','https:').split('#')[0] for u in refs if pattern in u and '.pdf' in u.lower()))
                    if len(matches)!=1:raise ValueError('Current official terms missing or ambiguous: '+name)
                    url=matches[0];host=urlsplit(p.url).hostname;target=urlsplit(url).hostname
                    if not (target==host or target.endswith('.'+host.removeprefix('www.'))):raise ValueError('Terms left the bank domain')
                    response=ctx.request.get(url,timeout=PAGE_LOAD_TIMEOUT_MS)
                    if not response.ok:raise ValueError('Terms HTTP '+str(response.status))
                    fn=batch.prefix+name+'.pdf';(batch.ev/fn).write_bytes(response.body())
                    with pymupdf.open(batch.ev/fn) as pdf:r=dict(url=url,text='\n'.join(pg.get_text() for pg in pdf))
                    doc=pymupdf.open(batch.ev/fn);ids=[];ev=[]
                    pdfpage=dict(id=batch.prefix+name,bank=bank,currency='multi',rate_type='promo',url=r['url'],ok=True,complete=True,captured_at=datetime.now(timezone.utc).isoformat(),images=[],image_hashes={},text=r['text'],sha256=hashlib.sha256(r['text'].encode()).hexdigest())
                    # Clauses that determine participation, amount, validity and channels.
                    for pi,pg in enumerate(doc):
                        for bi,block in enumerate(pg.get_text('blocks')):
                            text=block[4].strip()
                            if bank=='CITI' and 'All rates offered are promotional rates' in text:continue # Website verifies full rate grid; this PDF block cuts through its header.
                            if bank=='HSBC' and pi>0:continue # Eligibility is page 1; rates use complete website tables.
                            if not re.search(r'Promotion Period|Promotional Period|minimum|maximum|new or existing funds|Fresh Funds|Only personal|convert|conversion|USD 50,000|30,000|tenure under|from 01 September|from 3 August|no less than|existing DBS Personal',text,re.I):continue
                            if len(text.split())<15:continue # Fragmented PDF table captions are verified in the webpage table.
                            if len(text)>600:
                                lines=[l for bb in pg.get_text('dict')['blocks'] if bb.get('type')==0 for l in bb['lines'] if pymupdf.Rect(block[:4]).contains(pymupdf.Rect(l['bbox']))]
                                for start in range(0,len(lines),3):
                                    part=lines[start:start+3];literal='\n'.join(''.join(sp['text'] for sp in l['spans']) for l in part)
                                    if not literal.strip():continue
                                    rect=pymupdf.Rect(part[0]['bbox'])
                                    for l in part[1:]:rect|=pymupdf.Rect(l['bbox'])
                                    key=f'{name}-{pi}-{bi}-lines-{start}';image=batch.prefix+key+'.png'
                                    image=pdf_overview(pg,batch.ev,pdfpage['id'])
                                    ids.append(batch.task(pdfpage,key,literal,image,'text'));batch.tasks[-1]['comparison_profile']='promo-terms';ev.append(dict(page_id=pdfpage['id'],quote=literal,locator=key))
                                continue
                            key=name+'-'+str(pi)+'-'+str(bi);image=batch.prefix+key+'.png'
                            image=pdf_overview(pg,batch.ev,pdfpage['id'])
                            ids.append(batch.task(pdfpage,key,text,image,'text'));batch.tasks[-1]['comparison_profile']='promo-terms';ev.append(dict(page_id=pdfpage['id'],quote=text,locator=key))
                    (batch.ev/(pdfpage['id']+'.txt')).write_text(r['text'],encoding='utf8');batch.pages.append(pdfpage);doc.close();return ids,ev
                if bank=='BEA':
                    p,page=open_page('tier',link(bank));p.locator('table').nth(1).wait_for(state='visible')
                    tid,te=txt('tier-heading',r'^Tier Rates$');table=p.locator('table').nth(1);grid=table.evaluate(PHYSICAL)
                    selected=[i for i,row in enumerate(grid) if i<2 or (len(row)>2 and row[1]!='JPY')]
                    rows,ids=batch.table(p,page,'tier',table,selected,group_size=1)
                    nid,ne=txt('negotiation',r'^For fixed deposit amount of SGD500,000')
                    did,de=txt('source-date',r'As at \d+-[A-Za-z]+-\d{4}')
                    dt=re.search(r'\d+-[A-Za-z]+-\d{4}',de['quote'])[0]
                    section('tier',rows,ids+[tid,nid,did],[te,ne,de],source_category='Tier Rates',source_date=datetime.strptime(dt,'%d-%b-%Y').date().isoformat())
                    batch.finish(p,page);p.close()
                elif bank=='CIMB':
                    p,page=open_page('online',link(bank));table=p.locator('table').filter(has_text='Online Rates').first;rows,ids=batch.table(p,page,'online',table,group_size=1)
                    tid,te=txt('online-start',r'^From \d{1,2} [A-Za-z]+ 20\d{2}');mid,me=txt('online-minimum',r'^\*Online interest rates apply')
                    tids,ev=terms('online-terms','tnc-fcfd');section('online',rows,ids+[tid,mid]+tids,[te,me]+ev)
                    batch.finish(p,page);p.close()
                elif bank=='RHB':
                    p,page=open_page('campaign',link(bank));table=p.locator('table').filter(has_text='Currency of Account').first;rows,ids=batch.table(p,page,'campaign',table,group_size=1)
                    # Two official URLs resolve to the same PDF; select the canonical host path.
                    tids,ev=terms('campaign-terms','sg/dam/jcr:');section('campaign',rows,ids+tids,ev)
                    batch.finish(p,page);p.close()
                elif bank=='SBI':
                    p,page=open_page('usd',link(bank));table=p.locator('table').filter(has_text='Promotional Interest Rate').first;rows,ids=batch.table(p,page,'usd',table)
                    ev=[]
                    for key,pattern in [('renewal',r'Applicable for fresh and renewal'),('maximum',r'The minimum deposit amount as indicated'),('channel',r'Walk in to any')]:
                        i,e=txt(key,pattern);ids.append(i);ev.append(e)
                    section('usd',rows,ids,ev,currency='USD');batch.finish(p,page);p.close()
                elif bank=='SCB':
                    p,page=open_page('usd',link(bank));table=p.locator('table').filter(has_text='PROMOTIONAL INTEREST RATE').first;rows,ids=batch.table(p,page,'usd',table,group_size=1)
                    tid,te=txt('fresh-funds',r'For a limited time only, enjoy high returns')
                    tids,ev=terms('usd-terms','sg-usdtd-tnc');section('usd',rows,ids+[tid]+tids,[te]+ev,currency='USD');batch.finish(p,page);p.close()
                elif bank=='ICBC':
                    for cur,label in [('CNY','RMB Fixed Deposit'),('USD','USD Fixed Deposit')]:
                        p,page=open_page(cur,link(bank,'SGD'));p.get_by_text(label,exact=True).filter(visible=True).first.click();p.wait_for_timeout(PAGE_SETTLE_MS)
                        if cur=='USD':
                            heading=p.get_by_text(re.compile(r'^1\.USD Fixed Deposit promotional interest rate')).filter(visible=True).last
                        else:heading=p.get_by_text(re.compile(r'RMB Fixed Deposit promotional interest rate',re.I)).filter(visible=True).last
                        tid,te=batch.text(p,page,cur+'-title',heading);table=heading.locator('xpath=following::table[1]');rows,ids=batch.table(p,page,cur+'-promo',table,group_size=1)
                        ev=[te];ids.append(tid)
                        if cur=='CNY':
                            i,e=txt('CNY-minimum',r'^The minimum deposit amount over counter is RMB');ids.append(i);ev.append(e)
                        else:
                            i,e=txt('USD-minimum',r'Deposit over counter with minimum amount USD');ids.append(i);ev.append(e)
                        section(cur.lower(),rows,ids,ev,currency=cur);batch.finish(p,page);p.close()
                elif bank=='HSBC':
                    p,page=open_page('usd',link(bank));tids,ev=terms('usd-terms','hsbc-usd-time')
                    captured_audiences=[]
                    for aud in ['elite','wealth','premier','personal']:
                        names={'elite':r'^For Premier Elite customers:','wealth':r'^For Premier customers with wealth holdings','premier':r'^For Premier customers without wealth holdings:','personal':r'^For Personal Banking customers:'}
                        headings=p.get_by_text(re.compile(names[aud])).filter(visible=True)
                        # A withdrawn customer tier must disappear from the new
                        # Premier sheet, not abort capture or keep its old rate.
                        if not headings.count():continue
                        heading=headings.last
                        # The heading is now a caption INSIDE its table.
                        table=heading.locator('xpath=ancestor::table[1]')
                        if not table.count():table=heading.locator('xpath=following::table').filter(visible=True).first
                        rows,ids=batch.table(p,page,'usd-'+aud,table,group_size=1)
                        h,he=txt('audience-'+aud,names[aud]);section('usd-'+aud,rows,ids+[h]+tids,[he]+ev,currency='USD')
                        captured_audiences.append(aud)
                    if not captured_audiences:raise ValueError('HSBC USD customer-tier tables missing; source layout requires review')
                    batch.sections[-1]['captured_audiences']=captured_audiences
                    # App promotion displayed separately; retain its own terms and channels.
                    refs=p.locator('a[href]').evaluate_all('xs=>xs.map(x=>x.href)')
                    advertised=[cur for cur in ['AUD','GBP'] if p.get_by_text(re.compile(r'^\d\.\d+% p\.a\. on '+cur+r' Time Deposits$')).count()]
                    if advertised or any('hsbc-aud-gbp-time' in u for u in refs):
                        tids,ev=terms('aud-gbp-terms','hsbc-aud-gbp-time')
                        for cur in ['AUD','GBP']:
                            tid,te=txt(cur+'-rate',r'^\d\.\d+% p\.a\. on '+cur+r' Time Deposits$')
                            section(cur.lower(),[[te['quote']]],[tid]+tids,[te]+ev,currency=cur)
                    else:
                        batch.sections[-1]['omitted_currency_note']='HSBC本期官网只列USD促销，未列AUD/GBP促销；不沿用上次报价。'
                    batch.finish(p,page);p.close()
                elif bank=='DBS':
                    from market_rates.workbook_policy import workbook_policy
                    from market_rates.link_registry import entries,named_url
                    from market_rates.fx_promo import dbs_promo_links,dbs_inline_promotion
                    registry=[r for r in entries(bank_links()) if r['source_id']=='DBS-others-promo']
                    known=workbook_policy().get('known_no_promotion',{}).get('DBS-others-promo')
                    if known and len(registry)==1 and not registry[0]['url']:
                        p,page=open_page('public-status',named_url(known['public_source_key']))
                        body=p.locator('body').inner_text()
                        links=p.locator('a[href]').evaluate_all('xs=>xs.map(x=>({text:x.innerText,url:x.href}))')
                        new=dbs_promo_links(links)
                        if new or dbs_inline_promotion(body):raise ValueError('DBS官网出现外币促销内容，必须更新链接并完成报价核验：'+str(new))
                        heading=p.get_by_role('heading',name=re.compile('Fixed Deposit',re.I)).first
                        i,e=batch.text(p,page,'public-product-heading',heading)
                        section('source-unavailable',[],[i],[e],currency='USD',status='no_current_promotion',
                                confirmation_date=known['confirmed_on'],resolved_url=p.url,public_text=body,public_links=links)
                        batch.finish(p,page);p.close();r=batch.save();save(out/bank/'run.json',r)
                        print('DBS checked official public entry: no current independent FX promotion',flush=True)
                        continue
                    p,page=open_page('bonus',link('DBS'))
                    from market_rates.fx_promo import dbs_missing_marker
                    table=p.locator('table').filter(has_text='Bonus interest on top of board rate').first
                    # The bank can return HTTP 200 and redirect after DOM ready.
                    # Wait for either the actual promotion or an explicit error.
                    table.or_(p.get_by_text('Page you are trying to access is invalid',exact=True)).or_(p.get_by_text('Page not found',exact=True)).first.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
                    missing=dbs_missing_marker(p.url,p.locator('body').inner_text())
                    if missing:
                        i,e=txt('invalid-source','^'+re.escape(missing)+'$')
                        section('source-unavailable',[],[i],[e],currency='USD',status='source_unavailable',requested_url=link('DBS'),resolved_url=p.url)
                        batch.finish(p,page);p.close()
                        r=batch.save();save(out/bank/'run.json',r)
                        print(bank,'official promotion link invalid; recorded verified source absence',flush=True)
                        continue
                    rows,ids=batch.table(p,page,'bonus',table,group_size=1);ev=[]
                    p.get_by_text('How do I enjoy the bonus interest?',exact=True).click();p.wait_for_timeout(PAGE_SETTLE_MS)
                    for key,pattern in [('validity',r'^Offer ends \d{1,2} [A-Za-z]+'),('rate-composition',r'^\* Rate is as of'),('channel',r'^To enjoy the bonus interest on this USD')]:
                        i,e=txt(key,pattern);ids.append(i);ev.append(e)
                    tids,extra=terms('bonus-terms','retail-terms-and-conditions-usd');section('bonus',rows,ids+tids,ev+extra,currency='USD');batch.finish(p,page);p.close()
                elif bank=='BOC':
                    p,page=open_page('index',link('BOC'))
                    from urllib.parse import urljoin
                    from market_rates.boc_discovery import latest_promotion_url
                    refs=p.locator('a[href]').evaluate_all('els=>els.map(e=>({text:e.innerText,url:e.href}))')
                    url=latest_promotion_url(refs,datetime.now().strftime('%Y%m%d'));batch.finish(p,page,overview='viewport');p.close();p,page=open_page('mobile',url)
                    tables=p.locator('table').filter(has_text='手机银行(*新存单)');table=next(t for t in tables.all() if len(t.locator('table').all())==0)
                    grid=table.evaluate(PHYSICAL);ids=[]
                    first_fx=next((i for i,row in enumerate(grid) if row and row[0].strip() in ['美元','澳元','新西兰元','欧元','英镑','人民币']),None)
                    if first_fx is None:raise ValueError('BOC foreign currency table not found')
                    # Each merged source cell is verified once; no inferred repeat
                    # of merged currency/tenor labels is retained for human inspection.
                    for i,tr in enumerate(table.locator('tr').all()):
                        for j,cell in enumerate(tr.locator(':scope > td, :scope > th').all()):
                            text=cell.inner_text().strip()
                            if not text:continue
                            # The ordinary table has SGD then foreign currencies.
                            if 2<i<first_fx:continue
                            tid,_=batch.text(p,page,'cell-'+str(i)+'-'+str(j),cell);ids.append(tid)
                    ev=[]
                    for key,pattern in [('validity',r'^本次个人定期存款促销利率有效期'),('new-placement',r'^\*新存单是指'),('personal',r'^以上定期存款促销利率适用于个人客户')]:
                        i,e=txt(key,pattern);ids.append(i);ev.append(e)
                    section('mobile',grid,ids,ev);batch.finish(p,page,overview=table);p.close()
                elif bank=='CITI':
                    p,page=open_page('conversion',link(bank));table=p.locator('table').filter(has_text='1 Month Promotional').first
                    grid=table.evaluate(PHYSICAL);indexes=[i for i,row in enumerate(grid) if row[0]!='JPY'];rows,ids=batch.table(p,page,'conversion',table,indexes,group_size=1)
                    tids,ev=terms('conversion-terms','fcy-fxtd');section('conversion',rows,ids+tids,ev);batch.finish(p,page);p.close()
                    p,page=open_page('new-funds',link(bank,'SGD'));table=p.locator('table').filter(has_text='Currency / New Funds Time Deposit Amount').first
                    usd_cells=table.locator('tr').filter(has_text=re.compile(r'^USD\s*/')).evaluate_all('rs=>rs.map(r=>[...r.cells].map(c=>c.innerText.trim()))')
                    if len(usd_cells)!=1:raise ValueError('Citi USD source row missing or ambiguous')
                    values=usd_cells[0][1:]
                    if any(values[j].strip() for i,v in enumerate(values) if not v.strip() for j in range(i+1,len(values))):raise ValueError('Citi interior blank rate requires explicit column mapping')
                    rows,ids=batch.table(p,page,'new-funds',table,group_size=1)
                    # Conditions surrounding the table are separate literal tasks.
                    parent=table.locator('xpath=..');(batch.ev/'citi-new-funds-context.txt').write_text(parent.inner_text(),encoding='utf8')
                    ev=[]
                    for key,pattern in [('new-funds-validity',r'Enjoy attractive preferential rates on SGD and USD'),('new-funds-eligibility',r'These promotional rates are open to all existing Citibank customers')]:
                        i,e=txt(key,pattern);ids.append(i);ev.append(e)
                    section('new-funds',rows,ids,ev,currency='USD');batch.finish(p,page);p.close()
                else:raise ValueError('Adapter not implemented '+bank)
                r=batch.save();r['source_file_hashes']={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in batch.ev.glob('*.pdf')};save(batch.out/'run.json',r);print(bank,'COMPLETE',flush=True)
            except Exception as e:
                error_file=batch.out/'capture-error.json'
                save(error_file,dict(bank=bank,error=str(e)))
                record_failure(str(e) or type(e).__name__,bank=bank,step='采集',business='外币定存促销',log=error_file)
                failures.append(bank)
                print(bank,'FAILED',str(e)[:250],flush=True)
            finally:b.close()
    if failures:raise ValueError('Incomplete: '+','.join(failures))

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--out',required=True);a.add_argument('--banks',default='BEA,CIMB,RHB,SBI,SCB,ICBC,HSBC');x=a.parse_args();capture(x.out,x.banks.split(','))

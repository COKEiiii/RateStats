"""Live SPA tables, linked HSBC PDFs, and bounded original-pixel table tiles."""
from market_rates.link_registry import discovery_folder, official_url
from market_rates.failure_summary import bank_step, record_failure
from market_rates.browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS
import argparse,hashlib,re,shutil
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pymupdf
from market_rates.evidence_capture import pdf_overview
from playwright.sync_api import Error as BrowserError, sync_playwright
from market_rates.common import load,save,digest
from market_rates.pipeline import evidence_index
from scripts.capture_board_wave import PHYSICAL,shot
from urllib.parse import urlsplit

CIMB_HEADINGS=['US Dollar','Sterling Pound','Australian Dollar','New Zealand Dollar','Euro','Canadian Dollar','Renminbi (Offshore)']
SCB_HEADINGS=['U.S. DOLLARS','STERLING POUNDS','AUSTRALIAN DOLLARS','NEW ZEALAND DOLLARS','EURO','CANADIAN DOLLARS','HONGKONG DOLLARS','CHINESE REMINBI (CNH)']

def source_confirmation(bank,cur,url):
    rules=load(Path(__file__).resolve().parents[1]/'config/board-source-confirmations.json')['rules'];u=urlsplit(url)
    return next((r for r in rules if r['bank']==bank and r['scope']==cur and r['host']==u.hostname and r['path']==u.path),None)

def effective_date(text):
    match=re.search(r'Effective from:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})',text)
    if not match:raise ValueError('Maybank effective date missing')
    return datetime.strptime(match[1],'%d %B %Y').date().isoformat()

HSBC_CURRENCIES = r'USD|GBP|JPY|CAD|CHF|NZD|AUD|HKD|EUR|CNY'


def hsbc_column_header(row):
    return (len(row)>1 and ' '.join(row[0].split())=='Placement Amount / Term'
            and all(re.fullmatch(r'\d+ Months?', label) for label in row[1:]))


def hsbc_split_heading(page, after_table):
    """Find a dated currency title left below the last table on this page."""
    tail=page.get_text(clip=pymupdf.Rect(0,after_table,page.rect.width,page.rect.height),sort=True)
    match=re.search(r'Rates as at:\s*(\d{4}-\d{2}-\d{2})\s+([A-Za-z ]+? ('+HSBC_CURRENCIES+r'))\s*(?:# PUBLIC\s*)?$',tail)
    if match:
        return match[1],match[2],match[3]


def attach_hsbc_audiences(run,root):
    """Verify the PDF audience heading, in addition to each currency/date."""
    ev=Path(root)/'evidence'
    for page in run['pages']:
        if page['bank']!='HSBC' or not page['url'].split('?')[0].endswith('.pdf'):continue
        suffix=page['id'].rsplit('-',1)[1]
        source=ev/(page['id'].rsplit('hsbc-',1)[0]+'fcy-time-deposits-'+suffix+'.pdf')
        doc=pymupdf.open(source);pg=doc[0]
        blocks=[b for b in pg.get_text('blocks') if 'Foreign Currency Time Deposit Rates' in b[4]]
        if len(blocks)!=1:raise ValueError('HSBC audience heading missing')
        block=blocks[0];expected=block[4].strip();audience='Personal Banking' if suffix=='pb' else 'Premier'
        if audience not in expected:raise ValueError('HSBC PDF audience does not match the link')
        fn=page['id']+'-audience.png';rect=pymupdf.Rect(block[:4])+(-2,-2,2,2)
        fn=pdf_overview(pg,ev,page['id']);sha=hashlib.sha256((ev/fn).read_bytes()).hexdigest()
        tid=page['id']+'-audience';run['tasks'].append(dict(id=tid,bank='HSBC',kind='text',expected=expected,images=[fn],image_hashes={fn:sha},page_id=page['id']))
        if fn not in page['images']:page['images'].append(fn)
        page['image_hashes'][fn]=sha
        for section in run['sections']:
            if section['page_id']==page['id']:section['task_ids'].append(tid)
        run.setdefault('source_file_hashes',{})[source.name]=hashlib.sha256(source.read_bytes()).hexdigest()


def capture_hsbc_pdf(discovery,ev,prefix,sections,task,page_record):
    folder=discovery_folder(discovery,'HSBC-others-board')
    hsbc=load(folder/'source.json')
    filename='fcy-time-deposits-pb.pdf'
    source=folder/filename
    shutil.copy2(source,ev/(prefix+filename))
    url=next(link['url'] for link in hsbc['links'] if 'Foreign Currency Time Deposit rates' in link['text']
             and 'Personal Banking' in link['text'] and link['url'].split('?')[0].endswith('.pdf'))
    pid=prefix+'hsbc-pb'
    images=[]
    active=None
    pending=None
    seen=set()

    def add_task(page,key,expected,kind):
        image=pdf_overview(page,ev,pid)
        images.append(image)
        return task(key,'HSBC',expected,[image],pid,kind=kind)

    with pymupdf.open(source) as doc:
        for pi,pg in enumerate(doc):
            tables=pg.find_tables().tables
            for ti,table in enumerate(tables):
                rows=[[cell.strip() for cell in row if cell and cell.strip()] for row in table.extract()]
                rows=[row for row in rows if row]
                if not rows:
                    continue
                title_match=re.fullmatch(r'[A-Za-z ]+ ('+HSBC_CURRENCIES+r')',' '.join(rows[0][0].split())) if len(rows[0])==1 else None
                split=ti==0 and pending is not None and hsbc_column_header(rows[0])
                if pending and not split:
                    raise ValueError(f'HSBC PDF 第 {pi+1} 页缺少 {pending[2]} 的跨页表头')
                if title_match or split:
                    if split:
                        date,title,cur=pending
                        pending=None
                        title_page=doc[pi-1]
                        rows=[[title],*rows]
                    else:
                        cur=title_match[1]
                        title_page=pg
                        rect=table.bbox
                        context=pg.get_text(clip=pymupdf.Rect(rect[0],max(0,rect[1]-45),rect[2],rect[1]))
                        dates=re.findall(r'\d{4}-\d{2}-\d{2}',context)
                        if not dates:
                            raise ValueError(f'HSBC PDF 第 {pi+1} 页 {cur} 表缺少报价日期')
                        date=dates[-1]
                    if cur in seen or len(rows)<3 or not hsbc_column_header(rows[1]):
                        raise ValueError(f'HSBC PDF 第 {pi+1} 页 {cur} 表头重复或格式异常')
                    seen.add(cur)
                    active=dict(id=prefix+'hsbc-pb-'+cur,bank='HSBC',currency=cur,
                                product_id='hsbc-fx-personal-board',table=ti,layout='hsbc-fx-pdf',
                                audience='personal',source_date=date,rows=[],task_ids=[],page_id=pid)
                    data=rows[2:]
                else:
                    if active is None:
                        raise ValueError(f'HSBC PDF 第 {pi+1} 页存在无法归属币种的表格')
                    cur=active['currency']
                    data=rows
                width=len(rows[1] if title_match or split else active['rows'][1])
                if any(len(row)!=width or not row[0].startswith(cur) for row in data):
                    raise ValueError(f'HSBC PDF 第 {pi+1} 页 {cur} 表的续行币种或列数不符')
                active['rows']+=rows
                if cur=='JPY':
                    continue
                if title_match or split:
                    sections.append(active)
                for ri,row in enumerate(rows):
                    page=title_page if split and ri==0 else pg
                    key=f'hsbc-pb-p{pi+1}-t{ti}-part{ri}'
                    active['task_ids'].append(add_task(page,key,[row],'grid'))
                if title_match or split:
                    active['task_ids'].append(add_task(title_page,'hsbc-pb-'+cur+'-date',date,'text'))
            if tables:
                pending=hsbc_split_heading(pg,max(table.bbox[3] for table in tables))
        if pending:
            raise ValueError(f'HSBC PDF 末页缺少 {pending[2]} 的跨页数据')
        if not seen-{'JPY'}:
            raise ValueError('HSBC PDF 没有可用的外币挂牌表')
        page_record(pid,'HSBC',url,'\n'.join(page.get_text() for page in doc),images)
    print('HSBC personal PDF tables captured',flush=True)


def open_bea_board(context,url):
    """Let the registered entry page initialize the BEA rate form."""
    official_url(url,'BEA')
    for attempt in range(1,4):
        page=context.new_page()
        try:
            response=page.goto(url,wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
            status=response.status if response else None
            if status!=200:
                reason=f'HTTP {status}' if status is not None else '未收到 HTTP 响应'
                if status not in (None,408,429,500,502,503,504):
                    raise ValueError(f'BEA 官网入口请求失败（{reason}）：{url}')
            else:
                page.wait_for_function("""() => location.pathname.startsWith('/html/error/') ||
                    [...document.querySelectorAll('table')].some(table =>
                        table.getBoundingClientRect().width > 0 && table.rows.length > 2)""",
                    timeout=CONTENT_TIMEOUT_MS)
                official_url(page.url,'BEA')
                if not urlsplit(page.url).path.startswith('/html/error/'):
                    return page
                reason='官网跳转到错误页 '+page.url
        except BrowserError as exc:
            reason=str(exc).splitlines()[0]
        except BaseException:
            page.close()
            raise
        page.close()
        if attempt<3:
            print(f'BEA 挂牌页未加载（{reason}），从官网入口重试 {attempt+1}/3：{url}',flush=True)
    raise ValueError(f'BEA 官网挂牌表未加载（{reason}）：{url}')


def capture(discovery,out,banks=None):
    discovery=Path(discovery);out=Path(out);ev=out/'evidence';ev.mkdir(parents=True,exist_ok=False)
    pages=[];sections=[];tasks=[];now=datetime.now(timezone.utc).isoformat();today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    prefix=out.name+'-'
    def task(tid,bank,rows,image_names,pid,kind='grid',**extra):
        t=dict(id=prefix+tid,bank=bank,kind=kind,expected=rows,images=image_names,page_id=pid,**extra)
        t['image_hashes']={n:hashlib.sha256((ev/n).read_bytes()).hexdigest() for n in image_names};tasks.append(t);return t['id']
    def page_record(pid,bank,url,text,images):
        pages.append(dict(id=pid,bank=bank,currency='multi',rate_type='board',url=url,text=text,images=images,ok=True,complete=True,captured_at=now,sha256=hashlib.sha256(text.encode()).hexdigest(),image_hashes={n:hashlib.sha256((ev/n).read_bytes()).hexdigest() for n in images}))
        pages[-1]['images']=list(dict.fromkeys(images))
    specs=[('BEA-others-board','BEA','FX',[0,1]),('BEA-SGD-board','BEA','SGD',[0]),('Maybank-others-board','Maybank','SGD',[3]),('SCB-others-board','SCB','FX',list(range(8))),('CIMB-others-board','CIMB','FX',list(range(7)))]
    if banks:specs=[s for s in specs if s[1] in banks]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--disable-http2'])
        ctx=browser.new_context(viewport={'width':1440,'height':1600},device_scale_factor=2,locale='en-SG')
        try:
            for key,bank,cur,indices in specs:
                src=load(discovery_folder(discovery,key)/'source.json');p=None;pid=prefix+key
                try:
                    if bank=='BEA':
                        p=open_bea_board(ctx,src['url'])
                    else:
                        p=ctx.new_page()
                        response=p.goto(src.get('final_url',src['url']),wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
                        if not response or response.status!=200:raise ValueError('Source unavailable '+key)
                        p.locator('table').first.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
                    p.wait_for_timeout(PAGE_SETTLE_MS)
                    if bank=='SCB':
                        # The delayed consent banner has children with explicit
                        # visibility, so hiding its parent visibility is insufficient.
                        # Hide only this observed UI widget; never rate content.
                        p.add_style_tag(content='#onetrust-consent-sdk { display:none !important; }')
                    text=p.locator('body').inner_text();images=[]
                    (ev/(pid+'.html')).write_text(p.content(),encoding='utf8');(ev/(pid+'.txt')).write_text(text,encoding='utf8')
                    for index in indices:
                        table=p.locator('table').nth(index);table.scroll_into_view_if_needed();p.wait_for_timeout(PAGE_SETTLE_MS)
                        rows=table.evaluate(PHYSICAL);original=next(t['rows'] for t in src['tables'] if t['index']==index)
                        if rows!=original:raise ValueError('Source changed since discovery: '+key)
                        name=('maybank-sgd-board' if bank=='Maybank' else 'bea-'+('sgd' if cur=='SGD' else 'fx-'+('board' if index==0 else 'tier')) if bank=='BEA' else bank.lower()+'-fx-'+str(index))
                        kept=[];ids=[];batch=[];batch_rows=[]
                        overview=prefix+name+'-table.png';shot(p,table,ev/overview);images.append(overview)
                        def flush():
                            if not batch:return
                            if bank=='SCB' and len(batch)==1 and len(batch_rows[0])>4:
                                for start in range(0,len(batch_rows[0]),4):
                                    ids.append(task(name+'-'+str(len(ids)),bank,[batch_rows[0][start:start+4]],[overview],pid,comparison_profile='scb-range-separator'))
                            else:
                                ids.append(task(name+'-'+str(len(ids)),bank,list(batch_rows),[overview],pid,comparison_profile='scb-range-separator' if bank=='SCB' else 'literal'))
                                if all(len(row)==1 for row in batch_rows):tasks[-1].update(kind='text',expected='\n'.join(row[0] for row in batch_rows))
                            batch.clear();batch_rows.clear()
                        # Keep floating navigation/cookie layers outside the table hidden during tiles.
                        table.evaluate("e=>{window.__repairFloating=[...document.querySelectorAll('*')].filter(x=>x!==e&&!x.contains(e)&&!e.contains(x)&&['fixed','sticky'].includes(getComputedStyle(x).position)).map(x=>[x,x.style.visibility]);window.__repairFloating.forEach(([x])=>x.style.visibility='hidden')}")
                        for tr in table.locator('tr').all():
                            cells=tr.evaluate("r=>[...r.cells].map(c=>c.innerText.trim()).filter(Boolean)")
                            if not cells:continue
                            if 'JPY' in cells or any('Japanese Yen' in c for c in cells):flush();continue
                            if len(batch)==3:flush()
                            kept.append(cells);batch.append(tr);batch_rows.append(cells)
                        flush();p.evaluate("()=>window.__repairFloating.forEach(([x,v])=>x.style.visibility=v)")
                        section=dict(id=prefix+name,bank=bank,currency=cur,product_id=name,table=index,rows=kept,task_ids=ids,page_id=pid)
                        def metadata(label,loc):
                            fn=overview;expected=loc.inner_text().strip()
                            tid=task(name+'-'+label,bank,[[expected]],[fn],pid);tasks[-1].update(kind='text',expected=expected);ids.append(tid);images.append(fn);return expected
                        if bank=='BEA':
                            section['layout']='bea-fx' if cur=='FX' else 'bea-sgd';section['rate_basis']='annual_nominal'
                            date_loc=p.get_by_text(re.compile(r'^\(As at ')).filter(visible=True).first
                            dt=metadata('date',date_loc);m=re.search(r'\d{2}-[A-Z]{3}-\d{4}',dt);section['valid_from']=datetime.strptime(m[0],'%d-%b-%Y').date().isoformat()
                            if cur=='FX':
                                metadata('title',p.get_by_text('Board Rates' if index==0 else 'Tier Rates',exact=True).filter(visible=True).first)
                                metadata('limit',p.get_by_text('For fixed deposit amount of SGD500,000 equivalent or over, please contact us for the applicable prevailing interest rate.',exact=True).filter(visible=True).first)
                        if bank=='Maybank':section['valid_from']=effective_date(metadata('date',p.locator('table').nth(index+1)))
                        if bank=='CIMB':
                            heading=table.locator('xpath=preceding::h3[1]')
                            label=metadata('currency',heading)
                            if label!=CIMB_HEADINGS[index]:raise ValueError('CIMB currency order changed; review source mapping')
                            section['amount_mode']='quoted_point'
                        if bank=='SCB' and re.sub(r'\s+',' ',rows[0][0])!=SCB_HEADINGS[index]:raise ValueError('SCB currency order changed; review source mapping')
                        confirmation=source_confirmation(bank,cur,p.url)
                        if confirmation:section.update(rate_basis=confirmation['rate_basis'],unit_confirmation=confirmation)
                        sections.append(section)
                    page_record(pid,bank,p.url,text,images);print(bank,'HTML tables captured',flush=True)
                except Exception as exc:
                    record_failure(str(exc) or type(exc).__name__,bank=bank,step='采集',
                                   business='外币定存挂牌' if cur=='FX' else cur+'定存挂牌')
                    raise
                finally:
                    if p is not None:p.close()
        finally:browser.close()
    if not banks or 'HSBC' in banks:
        with bank_step('HSBC','采集',business='外币定存挂牌'):
            capture_hsbc_pdf(discovery,ev,prefix,sections,task,page_record)
    run=dict(id=out.name,as_of=today,bank_dates={p['bank']:today for p in pages},pages=pages,sections=sections,tasks=tasks,errors=[],evidence_hash=digest(pages),task_hash=digest(tasks),demo=False)
    run['source_file_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ev.iterdir() if p.suffix in {'.pdf','.html'}}
    attach_hsbc_audiences(run,out);run.update(evidence_hash=digest(pages),task_hash=digest(tasks))
    save(out/'run.json',run);evidence_index(pages,ev);print('Captured',len(sections),'sections',len(tasks),'tasks',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--discovery',required=True);p.add_argument('--out',required=True);p.add_argument('--banks');a=p.parse_args();capture(a.discovery,a.out,a.banks.split(',') if a.banks else None)

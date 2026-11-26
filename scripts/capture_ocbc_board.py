"""OCBC live board tables, linked terms and placement minima; original pixels."""
from market_rates.runtime import bank_links
from market_rates.browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS
import argparse, hashlib, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from market_rates.common import save, digest
from market_rates.link_registry import bank_link,named_url
from market_rates.pipeline import evidence_index
from scripts.capture_board_wave import PHYSICAL, shot

REQUIRED = {'SGD', 'USD', 'AUD', 'NZD', 'CAD', 'HKD', 'EUR', 'GBP'}


def reveal_interest_section(page, evidence_dir):
    """Reload a transiently incomplete terms page before reading its current text."""
    title=page.locator('#interest > .accordion__item__title')
    for attempt in range(3):
        try:
            title.wait_for(state='visible',timeout=20000)
            title.click(timeout=20000)
            return
        except PlaywrightTimeoutError:
            if attempt==2:
                (evidence_dir/'ocbc-terms-failed.html').write_text(page.content(),encoding='utf8')
                raise
            response=page.reload(wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
            if not response or response.status!=200:
                raise ValueError('OCBC terms source unavailable after reload')
            page.wait_for_timeout(PAGE_SETTLE_MS)


def capture(out, links):
    out = Path(out); ev = out/'evidence'; ev.mkdir(parents=True, exist_ok=False)
    stamp = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    prefix = out.name+'-'; pages = []; tasks = []; sections = []; coverage = []
    sgd=bank_link(links,'OCBC','SGD','board');fx=bank_link(links,'OCBC','others','board');registered=[sgd,fx]

    def add_task(page, name, expected, images, kind='grid'):
        t = dict(id=prefix+name, bank='OCBC', kind=kind, expected=expected,
                 images=images, page_id=page['id'], image_hashes={n:hashlib.sha256((ev/n).read_bytes()).hexdigest() for n in images})
        tasks.append(t); page['images'] += images; page['image_hashes'].update(t['image_hashes'])
        return t['id']

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-http2'])
        ctx = browser.new_context(viewport={'width':1800, 'height':1600}, device_scale_factor=2, locale='en-SG')
        def open_page(name, url):
            p = ctx.new_page(); response = p.goto(url, wait_until='domcontentloaded', timeout=PAGE_LOAD_TIMEOUT_MS)
            if not response or response.status!=200: raise ValueError('OCBC source unavailable: '+url)
            p.wait_for_timeout(PAGE_SETTLE_MS)
            record = dict(id=prefix+name, bank='OCBC', currency='multi', rate_type='board', url=p.url,
                          captured_at=stamp, ok=True, complete=True, images=[], image_hashes={})
            return p, record
        def finish(p, record,overview=None):
            record['text']=p.locator('body').inner_text(); record['sha256']=hashlib.sha256(record['text'].encode()).hexdigest()
            (ev/(record['id']+'.html')).write_text(p.content(),encoding='utf8')
            (ev/(record['id']+'.txt')).write_text(record['text'],encoding='utf8')
            if not record['images']:
                fn=record['id']+'-overview.png'
                if overview is not None:shot(p,overview,ev/fn)
                else:p.screenshot(path=str(ev/fn),full_page=True,animations='disabled',timeout=SCREENSHOT_TIMEOUT_MS)
                record['images']=[fn];record['image_hashes'][fn]=hashlib.sha256((ev/fn).read_bytes()).hexdigest()
            record['images']=list(dict.fromkeys(record['images']))
            pages.append(record); p.close()
        def metadata(p, record, name, locator):
            locator = locator.filter(visible=True).first
            text = locator.inner_text().strip()
            return add_task(record,name,text,[],'text'), dict(page_id=record['id'],quote=text,locator=name)
        def table_tasks(p, record, name, table):
            table.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
            rows=table.evaluate(PHYSICAL);ids=[]
            fn=prefix+name+'-table.png';shot(p,table,ev/fn)
            # Keep literal chunks small, but all chunks share the table overview.
            for i in range(0,len(rows),2):
                ids.append(add_task(record,name+'-'+str(i),rows[i:i+2],[fn]))
            if table.evaluate(PHYSICAL)!=rows:raise ValueError('OCBC table changed during capture')
            return rows,ids
        try:
            p, product = open_page('product',named_url('OCBC-product',links))
            p.locator('a[href="#document-apply-online"]').click(timeout=CONTENT_TIMEOUT_MS)
            minimum_loc=p.locator('#document-apply-online .item-list').filter(has=p.get_by_text('Minimum placement:',exact=True))
            minimum_task, minimum_evidence=metadata(p,product,'minimum',minimum_loc)
            if '$50,000 in HKD currency' not in minimum_evidence['quote']:raise ValueError('OCBC placement minima changed')
            finish(p,product,minimum_loc)

            p, terms = open_page('terms',named_url('OCBC-terms',links))
            reveal_interest_section(p,ev)
            basis_task, basis_evidence=metadata(p,terms,'annual-day-count',p.locator('p').filter(has_text=re.compile(r'^5\.1 Where applicable')))
            if '365-day or a 360-day basis' not in basis_evidence['quote']:raise ValueError('OCBC governing interest basis changed')
            finish(p,terms,p.locator('p').filter(has_text=re.compile(r'^5\.1 Where applicable')))

            p, record=open_page('sgd',sgd['url']); p.locator('table').first.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
            p.wait_for_function("()=>[...document.querySelectorAll('table')].some(t=>t.innerText.includes('S$5,000')&&t.innerText.includes('0.'))",timeout=CONTENT_TIMEOUT_MS)
            table=p.locator('table').first; rows, ids=table_tasks(p,record,'sgd-table',table)
            foot_task, foot_evidence=metadata(p,record,'sgd-renewal',p.locator('p').filter(has_text=re.compile(r'^\*Please note that new placements')))
            sections.append(dict(id=prefix+'sgd',bank='OCBC',currency='SGD',product_id='ocbc-sgd-board',layout='ocbc-sgd',rows=rows,
                task_ids=ids+[basis_task,foot_task],page_id=record['id'],rate_basis='annual_nominal',basis_evidence=[basis_evidence,foot_evidence]))
            finish(p,record); print('OCBC SGD captured',flush=True)

            p, record=open_page('fx',fx['url'])
            p.wait_for_function("()=>document.querySelectorAll('.foreign-currency table').length>=8 && [...document.querySelectorAll('.foreign-currency table')].every(t=>t.rows.length>=6)",timeout=CONTENT_TIMEOUT_MS)
            date_task,date_evidence=metadata(p,record,'fx-as-of',p.get_by_text(re.compile(r'^Daily prices as at')).filter(visible=True))
            m=re.search(r'\d{1,2} [A-Za-z]{3} \d{4}',date_evidence['quote'])
            if not m: raise ValueError('OCBC quote date missing')
            asof=datetime.strptime(m[0],'%d %b %Y').date().isoformat()
            unit_task,unit_evidence=metadata(p,record,'fx-annual',p.get_by_text(re.compile(r'^Rates quoted are on per annum basis')))
            channel_task,channel_evidence=metadata(p,record,'fx-online',p.get_by_text(re.compile(r'^Rates are only available from Mondays to Fridays',re.I)))
            published = set()
            for container in p.locator('.foreign-currency').all():
                heading=container.locator('h5').first
                label=heading.inner_text().strip(); cur=label.split(' - ',1)[0]
                published.add(cur)
                if cur not in REQUIRED-{'SGD'}:continue
                head_task,head_evidence=metadata(p,record,'fx-'+cur+'-currency',heading)
                rows,ids=table_tasks(p,record,'fx-'+cur,container.locator('table'))
                sections.append(dict(id=prefix+cur,bank='OCBC',currency=cur,product_id='ocbc-fx-online-board',layout='ocbc-fx',rows=rows,
                    task_ids=ids+[head_task,date_task,unit_task,channel_task,minimum_task],page_id=record['id'],rate_basis='annual_nominal',
                    valid_from=asof,minimum='50000' if cur=='HKD' else '5000',basis_evidence=[minimum_evidence,head_evidence,date_evidence,unit_evidence,channel_evidence]))
                print('OCBC',cur,'captured',flush=True)
            finish(p,record)
            captured = {s['currency'] for s in sections}
            expected = {'SGD'} | (published & REQUIRED)
            if captured != expected or len(sections) != len(expected):
                raise ValueError('OCBC published currencies incomplete')
            for cur in sorted(REQUIRED - captured):
                coverage.append(dict(bank='OCBC',currency=cur,product_id='ocbc-fx-online-board',
                    reason=f'官网完整页面 {asof} 未列出此币种；本轮未取得报价，未补造数值',
                    numeric_insertion=False,source=record['url'],source_date=asof,
                    published_currencies=sorted(published)))
        finally:browser.close()
    run=dict(id=out.name,as_of=today,bank_dates={'OCBC':today},pages=pages,sections=sections,tasks=tasks,
             errors=[],coverage=coverage,evidence_hash=digest(pages),task_hash=digest(tasks),demo=False,registered_links=registered)
    run['source_file_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ev.glob('*.html')}
    save(out/'run.json',run); evidence_index(pages,ev)
    print('Captured',len(sections),'sections',len(tasks),'local verification tasks',flush=True)

if __name__=='__main__':
    a=argparse.ArgumentParser(); a.add_argument('--out',required=True); a.add_argument('--links',default=str(bank_links())); x=a.parse_args();capture(x.out,x.links)

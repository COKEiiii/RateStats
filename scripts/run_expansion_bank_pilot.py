"""UOB / ICBC / BOC: discover current public promotion pages from the link book."""
from market_rates.browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS
from datetime import datetime,timezone,timedelta
from pathlib import Path
import re
from urllib.parse import urlsplit
from market_rates.common import load,save
from market_rates.failure_summary import bank_step
from market_rates.capture import allowed
from market_rates.link_registry import bank_link
from market_rates.boc_discovery import latest_promotion_url
from market_rates.source_inventory import BOC_SGD_BUNDLE

BANKS={'UOB':('uob-sgd-promo','UOB SGD Fixed Deposit Promotion'),
       'ICBC':('icbc-sgd-promo','ICBC SGD Fixed Deposit Promotion'),
       'BOC':('boc-sgd-mobile','BOC SGD Mobile Fixed Deposit')}

def boc_capture_units(page):
    """Bind mobile, welcome and counter bundle evidence to their own products."""
    table = '.trs_editor_view table:not(:has(table)):has-text("新元")'
    mobile_caption = ':has-text("手机银行"):has-text("新存单")'
    welcome_caption = ':has-text("手机银行"):has-text("仅限新客户")'
    bundle_caption = ':has-text("定期和储蓄通账户组合促销利率"):has-text("有效年利率")'
    bundle = table + bundle_caption
    bundle_count = page.locator(bundle).count()
    standalone = table + f':not({bundle_caption})'
    normal = standalone + mobile_caption + f':not({welcome_caption})'
    welcome = standalone + welcome_caption
    normal_count = page.locator(normal).count()
    welcome_count = page.locator(welcome).count()
    if normal_count != 1 or welcome_count > 1 or bundle_count > 1:
        raise ValueError('BOC promotion tables missing or ambiguous')
    if page.locator(standalone).count() != normal_count + welcome_count:
        raise ValueError('Unrecognized BOC SGD promotion table; requires review')
    units=[]
    if welcome_count:
        units.append(dict(selector=welcome,product_ids=['boc-sgd-welcome'],kind='rates'))
    units.append(dict(selector=normal,product_ids=['boc-sgd-mobile'],kind='rates',
                      end_before='tr:has(td:text-is("美元"))',end_before_text='美元'))
    for product in ['boc-sgd-mobile']+(['boc-sgd-welcome'] if welcome_count else []):
        units.append(dict(selector='.trs_editor_view > ul:has-text("本次个人定期存款促销利率有效期为")',
                          product_ids=[product],kind='terms'))
    if bundle_count:
        units.extend([
            dict(selector=bundle, product_ids=[BOC_SGD_BUNDLE], kind='rates'),
            dict(selector='.trs_editor_view > p:has-text("SmartSaver"):has-text("柜台")',
                 product_ids=[BOC_SGD_BUNDLE], kind='terms'),
        ])
    return units

def configuration(links,out):
    from playwright.sync_api import sync_playwright
    cfg=load('config/project.local.json');cfg['models'].update(num_ctx=32768,num_predict=8192)
    cfg.update(extraction_profile='multi-bank-literal-v1',transcribe_rate_units=True,expected_banks=list(BANKS),products=[],sources=[],
       tenor_map={'UOB/uob-sgd-promo/10M':'9M','BOC/boc-sgd-welcome/4M':'3M','BOC/boc-sgd-welcome/8M':'9M'},
       pilot=dict(no_publication=True,scope='UOB, ICBC, BOC SGD flat-rate promotions; board and step-up excluded'))
    out=Path(out);out.mkdir(parents=True)
    today=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        try:
            for bank,(pid,name) in BANKS.items():
                with bank_step(bank,'采集',business='新元定存促销'):
                    origin=bank_link(links,bank,'SGD','promo');cfg.setdefault('link_origins',[]).append(origin)
                    url=origin['url'];domain=urlsplit(url).hostname
                    domains=[domain]+(['v.icbc.com.cn'] if bank=='ICBC' else [])
                    cfg['products'].append(dict(id=pid,bank=bank,name=name,personal_status='unknown',expected=True))
                    page=browser.new_page(viewport={'width':1440,'height':1100},locale='en-SG')
                    try:
                        def guard(route):
                            if route.request.is_navigation_request() and not allowed(route.request.url,domains):route.abort()
                            else:route.continue_()
                        page.route('**/*',guard)
                        response=page.goto(url,wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
                        page.wait_for_timeout(PAGE_SETTLE_MS)
                        if not response or response.status>=400:raise ValueError('Official page unavailable: '+url)
                        refs=page.locator('a[href]').evaluate_all('els=>els.map(e=>({text:e.innerText,url:e.href}))')
                        save(out/(bank+'.json'),dict(url=url,links=refs,captured_at=datetime.now(timezone.utc).isoformat()))
                        if bank=='BOC':
                            url=latest_promotion_url(refs,today)
                            response=page.goto(url,wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
                            if not response or response.status>=400:raise ValueError('Official page unavailable: '+url)
                            page.locator('.trs_editor_view table').first.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
                            boc_units=boc_capture_units(page)
                            if any(BOC_SGD_BUNDLE in unit['product_ids'] for unit in boc_units):
                                terms_links = page.locator('.trs_editor_view a[href]').filter(
                                    has_text='Terms and Conditions Governing the BOCSG TD-MCS Bundle Promotion')
                                if terms_links.count() != 1:
                                    raise ValueError('BOC SmartSaver bundle terms missing or ambiguous')
                                boc_bundle_terms = terms_links.evaluate('link => link.href')
                    finally:page.close()
                    def source(key,u,**extra):
                        if not allowed(u,domains):raise ValueError('Unregistered official campaign domain: '+u)
                        return dict(id=key,bank=bank,enabled=True,urls=[u],domains=domains,max_depth=0,max_pages=1,product_ids=[pid],**extra)
                    def unit(selector,product=pid,kind='rates'):return dict(selector=selector,product_ids=[product],kind=kind)
                    if bank=='UOB':
                        cfg['sources'].append(source('UOB-rates',url,capture_units=[unit('#rates.uob-adaptive-table .desktop-table-container')],settle_ms=1500,dismiss_selectors=['.dy-custom-close-btn:visible']))
                        for tenor in [6,10,12]:
                            urls=list(dict.fromkeys(v['url'] for v in refs if v['text'].strip()==f'{tenor}-month Promotional Interest Rate'))
                            if len(urls)!=1:raise ValueError('Missing/ambiguous UOB terms')
                            s=source(f'UOB-terms-{tenor}',urls[0]);s['tenor_scope']=tenor;cfg['sources'].append(s)
                    elif bank=='ICBC':
                        cfg['sources'].append(source('ICBC-rates',url,capture_units=[
                            unit('#AD5con1 table:not(:has(table)):has-text("Counter Promotion")'),
                            dict(**unit('#AD5con1 table:not(:has(table)):has-text("E-Banking")'),split_table_rows=2)]))
                        urls=list(dict.fromkeys(v['url'] for v in refs if urlsplit(v['url']).path.endswith('/1fixeddepositpromotion.pdf')))
                        if len(urls)!=1:raise ValueError('Missing ICBC fixed deposit terms')
                        cfg['sources'].append(source('ICBC-terms',urls[0]))
                    else:
                        if any('boc-sgd-welcome' in u['product_ids'] for u in boc_units):
                            cfg['products'].append(dict(id='boc-sgd-welcome',bank=bank,name='BOC New Customer SGD Fixed Deposit',personal_status='unknown',expected=True))
                        cfg['sources'].append(source('BOC-rates',url,device_scale_factor=3,capture_units=boc_units))
                        if any(BOC_SGD_BUNDLE in unit['product_ids'] for unit in boc_units):
                            cfg['products'].append(dict(id=BOC_SGD_BUNDLE, bank=bank,
                                name='BOC SmartSaver 定存组合', personal_status='unknown', expected=True))
                            pdf_domains = ['pic.bankofchina.com']
                            if not allowed(boc_bundle_terms, pdf_domains):
                                raise ValueError('BOC SmartSaver bundle terms domain not registered')
                            cfg['sources'].append(dict(id='BOC-bundle-terms', bank=bank, enabled=True,
                                urls=[boc_bundle_terms], domains=pdf_domains, max_depth=0, max_pages=1,
                                product_ids=[BOC_SGD_BUNDLE], terms_role='primary'))
        finally:browser.close()
    return cfg

if __name__=='__main__':
    # Reuse the checked extraction/resume/evidence orchestration, not its bank config.
    import scripts.run_next_bank_pilot as runner
    runner.BANKS=BANKS;runner.configuration=configuration;runner.main()

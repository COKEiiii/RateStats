"""Bounded official-site discovery; archive text and necessary visual overviews."""
from __future__ import annotations
from market_rates.browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin, urldefrag
from .common import save
from .failure_summary import record_failure
from .workbook_policy import bank_in_scope


def allowed(url, domains):
    parsed = urlparse(url)
    return parsed.scheme in ("https", "http") and parsed.hostname in domains and not parsed.username and not parsed.password


def _region_nodes(page, selector):
    loc=page.locator(selector)
    loc.first.wait_for(state='attached',timeout=CONTENT_TIMEOUT_MS)
    nodes=[node for node in loc.all() if node.is_visible() or node.locator('*').filter(visible=True).count()]
    if not nodes:raise ValueError('Configured evidence region is not visible: '+selector)
    return nodes


def _section_text(text, section):
    lines=text.splitlines()
    starts=[i for i,line in enumerate(lines) if re.search(section['start'],line.strip(),re.I)]
    if len(starts)!=1:raise ValueError('Section start is missing or ambiguous')
    first=starts[0]
    ends=[i for i,line in enumerate(lines) if i>first and re.search(section['end'],line.strip(),re.I)]
    if not ends:raise ValueError('Section end is missing')
    last=ends[0];result='\n'.join(lines[first:last]).strip()
    if section.get('context'):
        context=[line.strip() for line in lines[:first] if re.search(section['context'],line.strip(),re.I)]
        if len(context)!=1:raise ValueError('Section context is missing or ambiguous')
        result=context[0]+'\n'+result
    if any(header not in result for header in section.get('table_headers',[])):
        raise ValueError('Section table headers are missing')
    return result,lines[first].strip(),lines[last].strip()


def _unit_text(nodes, unit):
    texts=[];cut=None
    for node in nodes:
        text=node.inner_text().strip()
        if unit.get('table_grid'):
            rows=node.evaluate("t=>[...t.rows].map(r=>[...r.cells].map(c=>c.innerText.trim()).filter(Boolean)).filter(r=>r.length)")
            if unit.get('table_row_range'):
                first,last=unit['table_row_range']
                rows=rows[:unit['table_header_rows']]+rows[first:last]
            text='\n'.join('\t'.join(row) for row in rows)
        if unit.get('end_before'):
            ending=node.locator(unit['end_before']).filter(visible=True).first
            if not ending.count():raise ValueError('Evidence boundary is missing: '+unit['end_before'])
            marker=ending.inner_text().strip()
            if not marker or marker not in text:raise ValueError('Evidence boundary does not match the visible text')
            text=text.split(marker,1)[0].strip();cut=ending
        texts.append(text)
    text='\n\n'.join(texts)
    if unit.get('section_between'):
        text,first,last=_section_text(text,unit['section_between'])
        return text,(first,last)
    if not text.strip():raise ValueError('Configured evidence region has no readable text')
    return text,cut


def _evidence_units(page,source):
    """Split configured wide rate tables without dropping any observed tenor row."""
    original=source.get('capture_units') or [dict(selector=selector) for selector in source.get('capture_selectors',[])]
    result=[]
    for unit in original:
        size=unit.get('split_table_rows')
        if size is None:
            result.append(unit);continue
        if not isinstance(size,int) or isinstance(size,bool) or size<1:
            raise ValueError('split_table_rows must be a positive integer')
        nodes=_region_nodes(page,unit['selector'])
        if len(nodes)!=1:raise ValueError('Split table selector must identify one table')
        rows=nodes[0].evaluate("t=>[...t.rows].map(r=>[...r.cells].map(c=>c.innerText.trim()).filter(Boolean)).filter(r=>r.length)")
        first=next((i for i,row in enumerate(rows) if row and re.fullmatch(r'\d+\s*(?:months?|years?|[MD])',row[0],re.I)),None)
        if first is None or first==0:raise ValueError('Split table has no bounded header and tenor rows')
        if any(not row or not re.fullmatch(r'\d+\s*(?:months?|years?|[MD])',row[0],re.I) or len(row)<2
               for row in rows[first:]):
            raise ValueError('Split table has an unexpected row; refusing to omit rates')
        for start in range(first,len(rows),size):
            part=dict(unit)
            part.pop('split_table_rows')
            part.update(table_grid=True,table_header_rows=first,table_row_range=[start,min(start+size,len(rows))])
            result.append(part)
    return result


def _region_image(page,nodes,cut,path):
    from .evidence_capture import table_overview
    if len(nodes)==1 and cut is None and nodes[0].is_visible():
        table_overview(page,nodes[0],path);return
    page.evaluate('window.scrollTo(0,0)')
    boxes=[]
    for node in nodes:
        box=node.bounding_box()
        if box and box['height']>0:boxes.append(box)
        else:
            boxes.extend(box for child in node.locator('*').filter(visible=True).all()
                         if (box:=child.bounding_box()) and box['height']>0)
    if not boxes:raise ValueError('Evidence region has no printable bounds')
    left=min(b['x'] for b in boxes);top=min(b['y'] for b in boxes)
    right=max(b['x']+b['width'] for b in boxes);bottom=max(b['y']+b['height'] for b in boxes)
    if isinstance(cut,tuple):
        first,last=cut
        start=page.get_by_text(first,exact=True).filter(visible=True).first.bounding_box()
        end=page.get_by_text(last,exact=True).filter(visible=True).first.bounding_box()
        if not start or not end:raise ValueError('Section screenshot bounds are unavailable')
        top=start['y'];bottom=end['y']
    elif cut is not None:
        box=cut.bounding_box()
        if not box:raise ValueError('Evidence cutoff is not visible')
        bottom=min(bottom,box['y'])
    left=max(0,left);top=max(0,top)
    if right<=left or bottom<=top:raise ValueError('Evidence screenshot bounds are empty')
    page.screenshot(path=str(path),clip=dict(x=left,y=top,width=right-left,height=bottom-top),
                    full_page=True,animations='disabled',timeout=SCREENSHOT_TIMEOUT_MS)


def _html_evidence(page,source,out,page_id,max_tiles):
    """Capture every configured region and bind its literal text to its image."""
    units=_evidence_units(page,source)
    if not units:
        body=page.locator('body').inner_text()
        if not body.strip():raise ValueError('HTML page has no readable text')
        height=page.evaluate('Math.max(document.documentElement.scrollHeight,document.body.scrollHeight)')
        if height>1100*max_tiles:raise ValueError('HTML overview exceeds capture limit; no truncation applied')
        name=f'{page_id}_overview.png'
        page.screenshot(path=str(out/name),full_page=True,animations='disabled',timeout=SCREENSHOT_TIMEOUT_MS)
        return body,[name],[],[],[]
    if len(units)>max_tiles:raise ValueError('Configured evidence units exceed capture limit')
    images=[];texts=[];unit_images=[];shared={}
    for index,unit in enumerate(units,1):
        nodes=_region_nodes(page,unit['selector']);text,cut=_unit_text(nodes,unit)
        key=(unit['selector'],unit.get('end_before'),str(unit.get('section_between')))
        name=shared.get(key)
        if name is None:
            name=f'{page_id}_region{index}.png';_region_image(page,nodes,cut,out/name)
            shared[key]=name;images.append(name)
        if compact(_unit_text(nodes,unit)[0])!=compact(text):raise ValueError('Configured evidence region changed during capture')
        texts.append(text);unit_images.append(name)
    return '\n\n'.join(texts),images,texts,unit_images,units


def capture_sources(config, out):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('安装采集依赖: pip install -e ".[live]"，再运行 python -m playwright install chromium') from exc
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    pages, errors = [], []
    sources = [s for s in config["sources"] if s.get("enabled") and bank_in_scope(s['bank'])]
    if not sources:
        raise ValueError("没有启用的官方来源，请先检查config/sources.json")
    with sync_playwright() as pw:
        if config.get('browser_channel')=='chrome':
            browser = pw.chromium.launch(channel='chrome',headless=False,args=['--start-minimized'])
        else:
            browser = pw.chromium.launch(headless=True,args=['--disable-http2'])
        try:
            for source in sources:
                domains = source["domains"]
                queue = [(url, 0) for url in source["urls"]]
                seen = set()
                count = 0
                scale=source.get('device_scale_factor',1)
                if scale not in [1,2,3]:raise ValueError('Unsupported capture pixel ratio')
                context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="en-SG",device_scale_factor=scale)
                # Navigation may not silently follow an off-domain replacement page.
                def route_handler(route):
                    if route.request.is_navigation_request() and not allowed(route.request.url, domains):
                        route.abort()
                    else:
                        route.continue_()
                context.route("**/*", route_handler)
                try:
                    while queue and count < source.get("max_pages", 8):
                        url, depth = queue.pop(0)
                        url = urldefrag(url)[0]
                        if url in seen or not allowed(url, domains):
                            continue
                        seen.add(url); count += 1
                        page_id = hashlib.sha256((source["id"] + url).encode()).hexdigest()[:20]
                        page = context.new_page()
                        images=[];region_texts=[];unit_images=[]
                        try:
                            # PDF attachment uses browser request, preserving the same session.
                            if urlparse(url).path.lower().endswith(".pdf"):
                                import pymupdf
                                response = context.request.get(url, timeout=PAGE_LOAD_TIMEOUT_MS, max_redirects=0)
                                pdf_url=url
                                for _ in range(3):
                                    if response.status not in (301,302,303,307,308):break
                                    target=urljoin(pdf_url,response.headers.get('location',''))
                                    if not allowed(target,domains) or target==pdf_url:raise ValueError('PDF redirect left registered domain or looped')
                                    pdf_url=target;response=context.request.get(pdf_url,timeout=PAGE_LOAD_TIMEOUT_MS,max_redirects=0)
                                if response.status != 200:
                                    raise ValueError(f"PDF HTTP {response.status}")
                                data = response.body()
                                if not data.startswith(b"%PDF"):
                                    raise ValueError("附件不是PDF")
                                (out / f"{page_id}.pdf").write_bytes(data)
                                doc = pymupdf.open(stream=data, filetype="pdf")
                                if len(doc) > config.get("max_pdf_pages", 30):
                                    raise ValueError("PDF页数超限，需要指定范围；未截断采集")
                                images = []
                                texts = []
                                for index, pdfpage in enumerate(doc):
                                    name = f"{page_id}_p{index+1}.png"
                                    pdfpage.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(out / name)
                                    images.append(name)
                                    texts.append(f"[PDF page {index+1}]\n" + pdfpage.get_text())
                                body, final, complete = "\n".join(texts), pdf_url, True
                            else:
                                for attempt in range(3):
                                    try:
                                        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                                        break
                                    except Exception:
                                        if attempt==2:raise
                                        page.wait_for_timeout(PAGE_SETTLE_MS)
                                if not response or response.status >= 400:
                                    raise ValueError(f"页面HTTP {response.status if response else '无响应'}")
                                if not allowed(page.url, domains):
                                    raise ValueError("跳转出已登记官方域名")
                                # Some sites reveal the body after their initial scripts
                                # settle. This explicit bounded delay is source-scoped.
                                settle = source.get('settle_ms', 0)
                                if type(settle) is not int or not 0 <= settle <= 30000:
                                    raise ValueError('settle_ms must be between 0 and 30000')
                                page.wait_for_timeout(max(PAGE_SETTLE_MS, settle))
                                # A zero-height body may contain visible positioned
                                # children (HLF). Test the selected content itself below.
                                page.locator("body").wait_for(state='attached', timeout=CONTENT_TIMEOUT_MS)
                                for selector in source.get("dismiss_selectors", [])+source.get("expand_selectors", []):
                                    for button in page.locator(selector).filter(visible=True).all():
                                        button.click(timeout=CONTENT_TIMEOUT_MS)
                                # Load lazy content using bounded scroll; do not bypass logins/CAPTCHAs.
                                last_height = 0
                                for _ in range(6):
                                    height=page.evaluate('Math.max(document.documentElement.scrollHeight,document.body.scrollHeight)')
                                    page.evaluate('window.scrollTo(0,document.documentElement.scrollHeight)')
                                    page.wait_for_timeout(SCROLL_SETTLE_MS)
                                    if height==last_height:break
                                    last_height=height
                                page.evaluate('window.scrollTo(0,0)')
                                # Archive after widgets became visible, so the HTML
                                # snapshot contains the same captured product region.
                                initial_body=page.locator('body').inner_text()
                                if re.search(r'verify you are human|access denied|captcha',initial_body,re.I):
                                    raise ValueError('访问验证或拦截页，需人工处理')
                                body,images,region_texts,unit_images,expanded_units=_html_evidence(page,source,out,page_id,config.get('max_tiles',30))
                                final_body=page.locator('body').inner_text()
                                if re.search(r'verify you are human|access denied|captcha',final_body,re.I):
                                    raise ValueError('访问验证或拦截页，需人工处理')
                                final=page.url
                                if not allowed(final,domains):raise ValueError('跳转出已登记官方域名')
                                complete=True
                                (out / f"{page_id}.html").write_text(page.content(), encoding="utf8")
                                (out / f"{page_id}.full.txt").write_text(final_body, encoding="utf8")
                                links = page.locator("a[href]").evaluate_all("els => els.map(e => ({url:e.href,text:e.innerText}))")
                                if depth < source.get("max_depth", 1):
                                    for link in links:
                                        if re.search(source.get("link_pattern", r"deposit|fixed|time.deposit|promotion|rates|\.pdf"),
                                                     link["text"] + " " + link["url"], re.I):
                                            candidate = urljoin(final, link["url"])
                                            if allowed(candidate, domains) and urldefrag(candidate)[0] not in seen:
                                                queue.append((candidate, depth + 1))
                            (out / f"{page_id}.txt").write_text(body, encoding="utf8")
                            record = {"id": page_id, "bank": source["bank"], "url": final, "requested_url": url,
                                          "captured_at": datetime.now(timezone.utc).isoformat(), "text": body,
                                          "images": images, "ok": True, "complete": complete,
                                          "capture_scope": "pdf" if urlparse(url).path.lower().endswith(".pdf") else ("configured_regions" if source.get("capture_selectors") else "full_page"),
                                          "capture_selectors": source.get("capture_selectors", []) if not urlparse(url).path.lower().endswith(".pdf") else [],
                                          "sha256": hashlib.sha256(body.encode()).hexdigest(),
                                          "image_hashes": {i: hashlib.sha256((out / i).read_bytes()).hexdigest() for i in images}}
                            if source.get('capture_units') and record['capture_scope'] != 'pdf':
                                record['capture_scope'] = 'configured_units'
                                record['units'] = [{**u, 'image': unit_images[n], 'text': region_texts[n]}
                                                   for n, u in enumerate(expanded_units)]
                            record['product_ids'] = source.get('product_ids', [])
                            record['terms_role'] = source.get('terms_role', 'primary')
                            pages.append(record)
                        except Exception as exc:
                            if not urlparse(url).path.lower().endswith('.pdf'):
                                try:(out / f'{page_id}.failure.html').write_text(page.content(), encoding='utf8')
                                except Exception:pass # Preserve the original navigation error.
                            errors.append(f'{source["bank"]} {url}: {type(exc).__name__}: {str(exc)[:250]}')
                            record_failure(str(exc) or type(exc).__name__,bank=source['bank'],step='采集')
                        finally:
                            page.close()
                    remaining = {urldefrag(u)[0] for u, _ in queue} - seen
                    if remaining:
                        reason=f'来源发现达到上限，仍有{len(remaining)}个候选链接待核查'
                        errors.append(source['bank']+reason)
                        record_failure(reason,bank=source['bank'],step='采集')
                finally:
                    context.close()
        finally:
            browser.close()
    save(out / "capture.json", {"pages": pages, "errors": errors})
    return pages, errors


def compact(s):
    return " ".join(s.split())

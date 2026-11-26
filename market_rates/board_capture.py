"""Text tasks with one shared overview per table, never per row or cell."""
import hashlib,re
from time import monotonic, sleep
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
from .common import save, digest
from .browser_wait import PAGE_LOAD_TIMEOUT_MS, CONTENT_TIMEOUT_MS, SCREENSHOT_TIMEOUT_MS, PAGE_SETTLE_MS, SCROLL_SETTLE_MS


class BoardCapture:
    def __init__(self,out,*,existing_evidence=False):
        self.out=Path(out);self.ev=self.out/'evidence';self.ev.mkdir(parents=True,exist_ok=existing_evidence)
        self.prefix=self.out.name+'-';self.pages=[];self.sections=[];self.tasks=[]
        self._progress_at=monotonic()
        self._table_images={}

    def open(self,context,bank,key,url,*,attempts=1):
        for attempt in range(1,attempts+1):
            p=context.new_page()
            try:
                p.set_default_timeout(CONTENT_TIMEOUT_MS)
                try:
                    resp=p.goto(url,wait_until='domcontentloaded',timeout=PAGE_LOAD_TIMEOUT_MS)
                except BrowserError as exc:
                    transient=isinstance(exc,BrowserTimeout) or bool(re.search(
                        r'net::ERR_(?:CONNECTION_RESET|CONNECTION_CLOSED|CONNECTION_TIMED_OUT|TIMED_OUT|NETWORK_CHANGED|HTTP2_PROTOCOL_ERROR)\b',
                        str(exc)))
                    if not transient or attempt==attempts:
                        raise
                    reason=str(exc).splitlines()[0]
                else:
                    status=resp.status if resp is not None else None
                    if status is not None and status<400:
                        p.wait_for_timeout(PAGE_SETTLE_MS)
                        page=dict(id=self.prefix+key,bank=bank,currency='multi',rate_type='board',url=p.url,ok=True,complete=True,
                            captured_at=datetime.now(timezone.utc).isoformat(),images=[],image_hashes={})
                        return p,page
                    reason=f'HTTP {status}' if status is not None else 'no HTTP response'
                    if status not in (None,408,429,500,502,503,504) or attempt==attempts:
                        raise ValueError(f'Source unavailable: {url} ({reason})')
            except BaseException:
                p.close()
                raise
            p.close()
            print(f'{bank} 挂牌页请求失败（{reason}），将重试 {attempt+1}/{attempts}：{url}',flush=True)
            sleep(2**(attempt-1))
        raise ValueError('attempts must be positive')

    def task(self,page,key,value,image,kind):
        hashes={image:hashlib.sha256((self.ev/image).read_bytes()).hexdigest()} if image else {}
        tid=self.prefix+key
        self.tasks.append(dict(id=tid,bank=page['bank'],kind=kind,expected=value,images=list(hashes),page_id=page['id'],image_hashes=hashes))
        if image and image not in page['images']:page['images'].append(image)
        page['image_hashes'].update(hashes)
        if len(self.tasks)==1 or len(self.tasks)%10==0 or monotonic()-self._progress_at>=45:
            print(f"{page['bank']} 已保存 {len(self.tasks)} 项文字核验任务，仍在采集：{key}",flush=True)
            self._progress_at=monotonic()
        return tid

    def text(self,p,page,key,loc):
        from .visible_html import visible_source_text
        loc=loc.filter(visible=True).first;txt=loc.inner_text().strip()
        visible=visible_source_text(loc.evaluate('e=>e.outerHTML'),txt)
        if visible:txt=visible['text']
        tid=self.task(page,key,txt,None,'text')
        if visible:self.tasks[-1]['hidden_accessibility_hints']=visible['removed']
        return tid,dict(page_id=page['id'],quote=txt,locator=self.prefix+key)

    def table(self,p,page,key,table,row_indices=None,group_size=2,transposed=False):
        from scripts.capture_board_wave import PHYSICAL
        table.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
        if transposed:
            # Render the off-screen table before asking Chromium for cell geometry.
            table.scroll_into_view_if_needed(timeout=CONTENT_TIMEOUT_MS)
        original=table.evaluate(PHYSICAL);rows=original
        trs=[tr for tr in table.locator('tr').all() if tr.evaluate('r=>[...r.cells].some(c=>c.innerText.trim())')]
        if len(trs)!=len(rows):raise ValueError('Physical row count changed')
        if transposed:
            if not trs or len(original[0]) < 2:
                raise ValueError('Transposed source table has fewer than two headers: '+key)
            # Wait for the same non-empty cells used by PHYSICAL, then measure
            # both in one browser call so a layout update cannot split the check.
            cells=trs[0].locator(':scope > th, :scope > td').filter(has_text=re.compile(r'\S'))
            try:
                cells.nth(0).wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
                cells.nth(1).wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
            except BrowserTimeout as exc:
                raise ValueError('Transposed source table headers did not render: '+key) from exc
            bounds=trs[0].evaluate("""row => [...row.cells]
                .filter(cell => cell.innerText.trim()).slice(0, 2).map(cell => {
                    const {x, y, width, height} = cell.getBoundingClientRect();
                    return {x, y, width, height};
                })""")
            if len(bounds)!=2 or any(not (box['width'] and box['height']) for box in bounds):
                raise ValueError('Transposed source table headers did not render: '+key)
            a,b=bounds
            if abs(a['x']-b['x'])>3 or b['y']<=a['y']:
                raise ValueError('Expected visually transposed source table: '+key)
        indices=list(range(len(rows))) if row_indices is None else list(row_indices)
        rows=[original[i] for i in indices]
        groups=[]
        for index in indices:
            if groups and index==groups[-1][-1]+1 and len(groups[-1])<group_size:groups[-1].append(index)
            else:groups.append([index])
        # A stable DOM identity shares one overview across currency subgroups.
        identity=table.evaluate("e=>{if(!e.dataset.rateEvidenceId)e.dataset.rateEvidenceId=String(window.__rateEvidenceCount=(window.__rateEvidenceCount||0)+1);return e.dataset.rateEvidenceId}")
        cachekey=(page['id'],identity)
        name=self._table_images.get(cachekey)
        if name is None:
            from .evidence_capture import table_overview
            name=self.prefix+key+'-table.png';table_overview(p,table,self.ev/name)
            if table.evaluate(PHYSICAL)!=original:
                raise ValueError('Source table changed while capturing evidence: '+key)
            self._table_images[cachekey]=name
        ids=[]
        for selected in groups:
            i=selected[0];values=[original[j] for j in selected]
            if transposed:values=[list(row) for row in zip(*values)]
            ids.append(self.task(page,key+'-'+str(i),values,name,'grid'))
            lengths=[len(row) for row in values]
            self.tasks[-1]['merged_caption_rows']=[n for n,row in enumerate(values) if len(row)==1 and re.search(r'[A-Za-z]',row[0])]
            if len(values)>1 and (len(set(lengths))>1 or max(lengths)==1):self.tasks[-1]['strict_row_layout']=True
        if table.evaluate(PHYSICAL)!=original:raise ValueError('Table changed during capture')
        return rows,ids

    def finish(self,p,page,overview=None):
        if not page['images']:
            name=page['id']+'-overview.png'
            if overview is not None and overview!='viewport':
                from .evidence_capture import table_overview
                table_overview(p,overview,self.ev/name)
            else:p.screenshot(path=str(self.ev/name),full_page=overview!='viewport',animations='disabled',timeout=SCREENSHOT_TIMEOUT_MS)
            page['images']=[name];page['image_hashes'][name]=hashlib.sha256((self.ev/name).read_bytes()).hexdigest()
        page['text']=p.locator('body').inner_text();page['sha256']=hashlib.sha256(page['text'].encode()).hexdigest()
        (self.ev/(page['id']+'.html')).write_text(p.content(),encoding='utf8')
        (self.ev/(page['id']+'.txt')).write_text(page['text'],encoding='utf8');self.pages.append(page)

    def save(self):
        from .pipeline import evidence_index
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        run=dict(id=self.out.name,as_of=today,bank_dates={p['bank']:today for p in self.pages},pages=self.pages,sections=self.sections,tasks=self.tasks,errors=[],evidence_hash=digest(self.pages),task_hash=digest(self.tasks),demo=False)
        run['source_file_hashes']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.ev.iterdir() if p.suffix in {'.pdf','.html'}}
        save(self.out/'run.json',run);evidence_index(self.pages,self.ev)
        print('Captured',len(self.sections),'sections;',len(self.tasks),'local verification tasks',flush=True)
        return run

"""Necessary visual overviews; model tasks are bound to text/HTML/PDF sources."""
from pathlib import Path
from .browser_wait import CONTENT_TIMEOUT_MS,SCREENSHOT_TIMEOUT_MS

def table_overview(page,locator,path):
    from playwright.sync_api import Error as PlaywrightError
    for attempt in range(3):
        locator.wait_for(state='visible',timeout=CONTENT_TIMEOUT_MS)
        locator.evaluate("e=>{window.__evidenceFloating=[...document.querySelectorAll('*')].filter(x=>x!==e&&!x.contains(e)&&!e.contains(x)&&['fixed','sticky'].includes(getComputedStyle(x).position)).map(x=>[x,x.style.visibility]);window.__evidenceFloating.forEach(([x])=>x.style.visibility='hidden')}")
        try:
            locator.screenshot(path=str(path),animations='disabled',timeout=SCREENSHOT_TIMEOUT_MS)
            return
        except PlaywrightError as exc:
            if 'not attached to the DOM' not in str(exc) or attempt==2:raise
            page.wait_for_timeout(300)
        finally:
            page.evaluate("()=>{(window.__evidenceFloating||[]).forEach(([x,v])=>x.style.visibility=v);delete window.__evidenceFloating}")

def pdf_overview(page,folder,prefix):
    """One complete preview per original PDF page, shared by all its tasks."""
    import pymupdf
    name=prefix+'-page-'+str(page.number+1)+'.png'
    path=Path(folder)/name
    if not path.exists():page.get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).save(path)
    return name

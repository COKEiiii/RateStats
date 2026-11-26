"""Opt-in local Chromium check for delayed and repeated product regions."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from market_rates.capture import capture_sources
from market_rates.common import save

def main():
    import argparse
    cli=argparse.ArgumentParser();cli.add_argument('--out',required=True);args=cli.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            delayed='<script>setTimeout(()=>document.body.insertAdjacentHTML("beforeend", "<div id=rates>SGD 6 Months 1.75% p.a.</div>"), 500)</script>'
            content='<html><body><h1>Local capture regression fixture</h1>'
            content+=delayed if self.path=='/delayed' else '<div id=rates>SGD 1.75%</div><div id=rates>USD 3.00%</div>'
            body=(content+'</body></html>').encode()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    results={}
    try:
        for case in ['delayed','ambiguous']:
            config=dict(sources=[dict(id=case,bank='SCB',enabled=True,domains=['127.0.0.1'],urls=[f'http://127.0.0.1:{server.server_port}/{case}'],max_pages=1,max_depth=0,capture_selectors=['#rates'])])
            # Force capture to arrive before the delayed widget exists.
            with patch('market_rates.capture.PAGE_SETTLE_MS',0),patch('market_rates.capture.SCROLL_SETTLE_MS',0),patch('market_rates.capture.CONTENT_TIMEOUT_MS',3000):
                pages,errors=capture_sources(config,out/case)
            if case=='delayed':
                assert not errors and len(pages)==1 and pages[0]['text']=='SGD 6 Months 1.75% p.a.',(pages,errors)
                assert 'SGD 6 Months 1.75% p.a.' in (out/case/(pages[0]['id']+'.full.txt')).read_text(encoding='utf8')
                assert '<div id="rates">' in (out/case/(pages[0]['id']+'.html')).read_text(encoding='utf8')
            else:
                assert not errors and len(pages)==1,(pages,errors)
                assert 'SGD 1.75%' in pages[0]['text'] and 'USD 3.00%' in pages[0]['text']
                assert len(pages[0]['images'])==1 and (out/case/pages[0]['images'][0]).stat().st_size>0
            results[case]=dict(passed=True,pages=len(pages),errors=errors)
    finally:server.shutdown();server.server_close()
    save(out/'checks.json',results);print(json.dumps(results,ensure_ascii=False))

if __name__=='__main__':main()

"""Check independent dependencies without touching the original project."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
from market_rates.common import load,save
from market_rates.runtime import ROOT,ensure_configuration,bank_links
from scripts.platform_runtime import excel_command,prepare_environment

def check(*,browser=True,excel=True,model=True):
    ensure_configuration();checks=[]
    def test(name,fn):
        try:result=fn();checks.append(dict(name=name,passed=True,details=result))
        except Exception as exc:checks.append(dict(name=name,passed=False,error=str(exc)))
    def python():
        if sys.version_info<(3,11):raise ValueError('需要Python 3.11或更新版本')
        return dict(executable=sys.executable,version=sys.version)
    test('Python',python)
    for package in ('openpyxl','playwright','PyMuPDF'):test(package,lambda p=package:importlib.metadata.version(p))
    def assets():
        from market_rates.input_workbooks import configuration,validate
        from market_rates.delivery import prepare_templates
        from market_rates.runtime import project_path
        from market_rates.report_xml import Book
        selected=configuration()
        # Latest mode may use defaults only before the first completed run.
        report=selected.get('report') or str(ROOT/'inputs/baseline-report.xlsx')
        rainbow=selected.get('rainbow') or str(ROOT/'inputs/baseline-rainbow.xlsx')
        validate(report,rainbow)
        if not bank_links().is_file():raise ValueError('银行链接表不存在')
        templates=load(ROOT/'config/format-templates.local.json')
        for kind in ('report','rainbow'):Book(project_path(templates[kind]))
        return dict(report=report,rainbow=rainbow,bank_links=str(bank_links()))
    test('模板与两份完整底稿',assets)
    if browser:
        def browser_check():
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b=pw.chromium.launch(headless=True)
                try:return dict(version=b.version)
                finally:b.close()
        test('Chromium',browser_check)
    if excel:
        def excel_check():
            target=ROOT/'data'/'excel-environment-check.pdf'
            target.parent.mkdir(exist_ok=True)
            subprocess.run(excel_command('excel-smoke',target),check=True,timeout=90,env=prepare_environment())
            if not target.is_file():raise RuntimeError('Excel preview export failed')
            target.unlink()
            return dict(application='Microsoft Excel for Mac',automation='xlwings/appscript')
        test('桌面Excel',excel_check)
    if model:
        from market_rates.model_adapter import preflight
        test('本机文字LLM',lambda:preflight(load(ROOT/'config/project.local.json')))
    return dict(passed=all(c['passed'] for c in checks),extraction_mode='llm_only',local_only=True,project=str(ROOT),checks=checks)

def main():
    p=argparse.ArgumentParser();p.add_argument('--no-browser',action='store_true');p.add_argument('--no-excel',action='store_true');p.add_argument('--no-model',action='store_true');p.add_argument('--out');a=p.parse_args()
    result=check(browser=not a.no_browser,excel=not a.no_excel,model=not a.no_model)
    if a.out:save(a.out,result)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())

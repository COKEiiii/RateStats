"""Source-bound checks for one text result. No second model result is inferred."""
from collections import Counter
from decimal import Decimal
import re
from .schema import normalize, key, group_key
from .common import digest,load
from pathlib import Path

CONTRACT='llm-only-v1'


def _verified_official_absence(item,wave,folder):
    """Accept an observed no-quote only when the complete official inventory proves it."""
    if (item.get('bank')!='OCBC' or item.get('product_id')!='ocbc-fx-online-board'
            or item.get('numeric_insertion') is not False):return False
    currencies=item.get('published_currencies')
    source_date=item.get('source_date');source=item.get('source')
    if not isinstance(currencies,list) or item.get('currency') in currencies or not source_date or not source:return False
    expected=f'官网完整页面 {source_date} 未列出此币种；本轮未取得报价，未补造数值'
    if item.get('reason')!=expected:return False
    pages=[p for p in wave.get('pages',[]) if p.get('bank')=='OCBC' and p.get('url')==source]
    if len(pages)!=1:return False
    page=pages[0]
    if page.get('id')!='ocbc-fx' or not page.get('ok') or not page.get('complete'):return False
    if digest(wave.get('pages',[]))!=wave.get('evidence_hash'):return False
    try:
        from datetime import datetime,timezone,timedelta
        from .pipeline import check_evidence
        from .table_validation import sha
        root=Path(folder)
        check_evidence(wave,root/'evidence')
        captured=datetime.fromisoformat(page['captured_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        if captured!=source_date:return False
        match=re.search(r'Daily prices as at\s+(\d{1,2} [A-Za-z]{3} \d{4})',page.get('text',''))
        if not match or datetime.strptime(match[1],'%d %b %Y').date().isoformat()!=source_date:return False
        observed=set(re.findall(r'^\s*([A-Z]{3})\s+-\s+[^\n]+',page.get('text',''),re.M))
        if observed!=set(currencies):return False
        html_name=page['id']+'.html';expected_hash=wave.get('source_file_hashes',{}).get(html_name)
        return bool(expected_hash and sha(root/'evidence'/html_name)==expected_hash)
    except (KeyError,TypeError,ValueError,OSError):return False


def require_complete_sources(run):
    from .workbook_policy import workbook_policy,bank_in_scope,currency_in_scope
    from .failure_summary import record_failure
    policy=workbook_policy()
    if not policy.get('complete_public_quotes'):return
    for path in run.get('wide_sources',[])+run.get('fx_sources',[]):
        wave=load(Path(path)/'run.json')
        failures=wave.get('errors',[])+wave.get('qualification_capture_errors',[])
        for item in wave.get('coverage',[]):
            if item.get('numeric_insertion') is not False or not bank_in_scope(item.get('bank',''),policy):
                continue
            currency=item.get('currency')
            # Source-wide failures have no single currency. Only exclude an
            # explicitly identified out-of-scope currency.
            if isinstance(currency,str) and re.fullmatch('[A-Za-z]{3}',currency.strip()):
                if not currency_in_scope(currency,policy):
                    continue
            if not _verified_official_absence(item,wave,Path(path)):
                failures.append(item)
        if failures:
            for item in failures:
                if isinstance(item,dict):
                    reason=item.get('error') or item.get('reason') or '来源未完成'
                    record_failure(reason,bank=item.get('bank'),
                                   step='核验' if item.get('failed_tasks') else '采集',
                                   log=Path(path)/'run.json')
            raise ValueError('来源尚有采集或核验缺项，禁止将部分报价标为完整：'+str(path)+' '+str(failures))

def require_text_run(run):
    if 'vlm' in run:raise ValueError('旧视觉／双路运行记录不能用于纯LLM版本，请重新采集')
    if not isinstance(run.get('llm'),dict):raise ValueError('缺少LLM提取结果')

def normalized_row_hashes(run):
    """Rebuild derived table rows from their verified source grids, never guesses.

    A quote such as 'amount | tenor | rate' is a table projection, not a
    contiguous passage. Accept it only if deterministic re-normalization of
    a hash-checked LLM source wave produces the entire identical offer.
    """
    from .board_wave import validate_wave,normalize_section
    pages={p['id']:digest(p) for p in run.get('pages',[])}
    def source(path):
        wave=validate_wave(path)
        if any(pages.get(p['id'])!=digest(p) for p in wave['pages']):
            raise ValueError('合并报价的来源页与已核验原件不符')
        return wave
    rows=[]
    for path in run.get('wide_sources',[]):
        wave=source(path)
        rows.extend(r for section in wave['sections'] for r in normalize_section(section,'llm'))
    if not run.get('fx_sources'):
        from .board_source_inventory import direct_board_rows
        covered={(r['bank'],r['currency'],r['product_id']) for r in rows}
        # Refreshed wave adapters supersede an older legacy capture of the
        # same product. Its archived pages remain evidence, never extra tiers.
        rows.extend(r for r in direct_board_rows(run.get('pages',[])) if (r['bank'],r['currency'],r['product_id']) not in covered)
    if run.get('fx_sources'):
        from .fx_promo import normalize_fx
        from .pipeline import check_evidence
        from .table_validation import sha
        board_path=Path(run['board_dependency'])
        if sha(board_path/'run.json')!=run['board_dependency_sha']:raise ValueError('外币加点依赖的挂牌来源已变化')
        board=load(board_path/'run.json');require_text_run(board)
        check_evidence(board,board_path/'evidence')
        board_rows,blocked=validated_offers(board)
        if blocked:raise ValueError('外币促销依赖的挂牌结果未通过')
        if any(pages.get(p['id'])!=digest(p) for p in board['pages']):raise ValueError('挂牌依赖来源页不匹配')
        for path in run['fx_sources']:
            wave=source(path)
            rows.extend(r for section in wave['sections'] for r in normalize_fx(section,board_rows))
    return {digest(normalize(row)) for row in rows}


def offer_problems(row,run,source_rows=None):
    problems=[]
    try: row=normalize(row)
    except (ValueError,TypeError,KeyError) as exc:return ['报价结构错误：'+str(exc)]
    if row['rate_pct'] is not None and not Decimal('0')<=Decimal(row['rate_pct'])<=Decimal('25'):problems.append('利率数值超出允许范围')
    pages={p['id']:p for p in run.get('pages',[])}
    projected=digest(row) in (source_rows if source_rows is not None else normalized_row_hashes(run))
    compact=lambda x:re.sub(r'\s+','',str(x))
    for e in row['evidence']:
        page=pages.get(e['page_id'])
        if not page or not page.get('ok') or not page.get('complete') or page.get('bank')!=row['bank']:
            problems.append('来源缺失、不完整或银行不符');continue
        if not e.get('locator') or not str(e.get('quote','')).strip():problems.append('缺少原文摘录或定位');continue
        if not projected and compact(e['quote']) not in compact(page.get('text','')):problems.append('原文摘录不能在采集文字中定位')
    # Bank-specific parsers bind row/column, units and derived formulas before
    # this gate. Also reject a changed numeric result with unchanged evidence.
    numbers={Decimal(n.replace(',','')) for e in row['evidence']
             for n in re.findall(r'(?<![\d.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.])',e['quote'])}
    if row['rate_pct'] is not None and Decimal(row['rate_pct']) not in numbers:
        problems.append('利率数值不能在来源摘录中定位')
    return sorted(set(problems))

def validated_offers(run):
    require_text_run(run)
    require_complete_sources(run)
    rows=run['llm'].get('offers',[]);counts=Counter(key(r) for r in rows)
    from .source_inventory import verify_source_inventory
    verify_source_inventory(rows,run.get('pages',[]))
    source_rows=normalized_row_hashes(run)
    actual_rows={digest(normalize(r)) for r in rows}
    missing=source_rows-actual_rows
    if missing:raise ValueError(f'来源表格覆盖不足：{len(missing)} 条已核验来源报价未进入明细')
    unexpected=actual_rows-source_rows if source_rows else set()
    if unexpected:raise ValueError(f'来源表格覆盖不一致：{len(unexpected)} 条明细没有对应的已核验来源行')
    blocked={group_key(r) for r in rows if counts[key(r)]!=1 or offer_problems(r,run,source_rows)}
    result=run['llm']
    if result.get('coverage_complete') is not True or result.get('unreadable'):
        blocked.update(group_key(r) for r in rows)
    return [r for r in rows if group_key(r) not in blocked],sorted(blocked)

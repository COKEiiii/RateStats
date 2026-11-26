"""Shared retail eligibility and stable maximum selection for every output."""
from decimal import Decimal,InvalidOperation
import re

PERSONAL = {'personal', 'all', '个人', '普通个人', '个人客户', '所有个人客户',
            '所有客群', '所有客户', 'personal banking'}


def comparison_eligible(row):
    # A legacy view lacking a currency layout is not a customer restriction.
    return not row.get('hold_reason') or row['hold_reason']=='现有模板无CHF区域，仅留明细'


def is_personal(audience):
    return str(audience or '').strip().casefold() in PERSONAL


def personal_offer(records):
    eligible = [r for r in records if is_personal(r.get('audience')) and r.get('rate_pct') is not None]
    if not eligible:
        return None
    highest = max(Decimal(r['rate_pct']) for r in eligible)
    tied = [r for r in eligible if Decimal(r['rate_pct']) == highest]
    def stable(row):
        number=row.get('input_row')
        return number if isinstance(number,int) and number>0 else eligible.index(row)
    if len({r.get('amount_currency', r.get('currency')) for r in tied}) == 1:
        # Unknown minimum is not a zero placement threshold.
        return min(tied, key=lambda r: (Decimal(r['amount_min']) if r.get('amount_min') is not None else Decimal('Infinity'),stable(r)))
    return min(tied,key=stable)


def personal_rows(records):
    return [r['input_row'] for r in records if is_personal(r.get('audience')) and r.get('rate_pct') is not None]


def summary_terms(selections):
    """Describe only each selected record; never borrow a cheaper tier's terms."""
    from .input_sheets import CHANNELS
    amounts=[];notes=[]
    for tenor,row in selections:
        if row is None:
            amounts.append(tenor+'：-');notes.append(tenor+'：无合资格Personal报价');continue
        bounds=[]
        if row.get('amount_min') is not None:bounds.append(('≥' if row['min_inclusive'] else '>')+format(Decimal(row['amount_min']),',f'))
        if row.get('amount_max') is not None:bounds.append(('≤' if row['max_inclusive'] else '<')+format(Decimal(row['amount_max']),',f'))
        amounts.append(tenor+'：'+row['amount_currency']+' '+('；'.join(bounds) if bounds else '起存金额未列明'))
        parts=[CHANNELS.get(row['channel'],row['channel']),{'yes':'新资金','no':'无新资金限制','unknown':'新资金要求见条款'}[row['fresh_funds']]]
        if row.get('valid_from') or row.get('valid_to'):parts.append(str(row.get('valid_from') or '未注明起日')+'–'+str(row.get('valid_to') or '未列截止日'))
        if row.get('reference_quote'):parts.append('公告已截止，仅参考')
        notes.append(tenor+'：'+'；'.join(parts))
    return '\n'.join(amounts) or '-',('完整条件及来源见报价明细。\n'+'\n'.join(notes)) if notes else '无合资格Personal报价'


def personal_quote(quotes):
    """Apply the identical selector to the formatters' quote objects."""
    def minimum(value):
        try:
            result=Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation,ValueError):return None
    rows=[dict(audience=q.audience,rate_pct=q.rate,amount_min=minimum(q.minimum),
               amount_currency=getattr(q,'amount_currency','') or getattr(q,'currency','SGD'),
               input_row=getattr(q,'source_row',None) or getattr(q,'row',None),_quote=q) for q in quotes]
    selected=personal_offer(rows)
    return selected['_quote'] if selected else None


def caption_is_personal(label):
    """Read explicit current captions; retain ordinary unlabelled history."""
    first=str(label or '').splitlines()[0] if label else ''
    m=re.search(r'(?:/|；|\()\s*(personal|all|unknown|premier(?:_\w+)?|preferred|private|corporate)(?=\s|/|；|\)|$)',first,re.I)
    if m:return is_personal(m[1])
    if re.search(r'\bPersonal Banking\b',first,re.I):return True
    return not bool(re.search(r'\b(?:priority|premier|preferred|private|corporate|citigold|prestige)\b|卓越|私人|贵宾|企业',first,re.I))

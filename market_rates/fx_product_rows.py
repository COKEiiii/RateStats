"""Consolidate identical FX quotes and reuse their historical product rows.

Raw evidence is never removed: a displayed interval records all source input rows.
An amount/rate/eligibility change is not an exact duplicate. Only adjacent amounts
with the same price and every other financial condition can form one interval.
"""
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal


def source_rows(offer):
    return offer.get('source_offers', [offer])


def equal_rate_coverage(target, tiers):
    """Return one prior rate only when known equal-rate tiers cover the target.

    Intervals are (lower, upper, lower_inclusive, upper_inclusive), with a known
    lower amount and None as an explicitly open upper end. Callers must filter
    bank, currency, tenor, product and eligibility before invoking this helper.
    The same touching-boundary rule is used by ``consolidate_usd`` below: at
    least one side must include the shared boundary. A missing minimum is never
    assumed to be zero, and a finite upper bound cannot cover an open range.
    """
    if target is None or target[0] is None:return None
    lo,hi,li,ui=target
    if hi is not None and (lo>hi or (lo==hi and not (li and ui))):return None
    clipped=[]
    for interval,rate in tiers:
        if interval is None or interval[0] is None:continue
        left,right,left_in,right_in=interval
        if right is not None and (left>right or (left==right and not (left_in and right_in))):continue
        # Intersect with the requested amount range. Old tiers may extend past
        # its outside edges, but cannot bridge an internal eligibility gap.
        if left<lo:left,left_in=lo,li
        elif left==lo:left_in=left_in and li
        if hi is not None:
            if right is None or right>hi:right,right_in=hi,ui
            elif right==hi:right_in=right_in and ui
        if right is not None and (left>right or (left==right and not (left_in and right_in))):continue
        clipped.append((left,right,left_in,right_in,rate))
    if len(clipped)<2 or len({r[4] for r in clipped})!=1:return None
    clipped.sort(key=lambda r:(r[0],not r[2],r[1] if r[1] is not None else Decimal('Infinity')))
    first=clipped[0]
    if first[0]!=lo or (li and not first[2]):return None
    end,end_in=first[1],first[3]
    for left,right,left_in,right_in,_ in clipped[1:]:
        if end is None:break
        if left>end or (left==end and not (end_in or left_in)):return None
        if right is None or right>end:end,end_in=right,right_in
        elif right==end:end_in=end_in or right_in
    complete=(end is None if hi is None else end is not None and end>=hi and (end>hi or not ui or end_in))
    return first[4] if complete else None


def consolidate_usd(offers):
    fields = ('bank', 'product_id', 'currency', 'rate_type', 'tenor_value',
              'tenor_unit', 'audience', 'channel', 'amount_currency',
              'amount_is_equivalent', 'fresh_funds', 'conditions', 'rate_pct',
              'rate_basis', 'valid_from', 'valid_to', 'availability')
    groups = defaultdict(list)
    output = []
    for offer in offers:
        if offer['currency'] != 'USD':
            output.append(offer)
            continue
        groups[tuple(offer.get(key) for key in fields)].append(offer)
    audit = []
    for rows in groups.values():
        rows.sort(key=lambda r: (Decimal(r['amount_min']) if r['amount_min'] is not None else Decimal('-Infinity'),
                                 not r['min_inclusive'],
                                 Decimal(r['amount_max']) if r['amount_max'] is not None else Decimal('Infinity')))
        current = None
        for row in rows:
            if current is None:
                current = dict(row, source_offers=source_rows(row))
                continue
            hi, lo = current['amount_max'], row['amount_min']
            # A missing lower bound is unknown, not negative infinity. Require an
            # exact amount identity for that case, never infer a continuous band.
            exact = all(current[k] == row[k] for k in ('amount_min', 'amount_max', 'min_inclusive', 'max_inclusive'))
            joined = (current['amount_min'] is not None and lo is not None and
                      (hi is None or Decimal(hi) > Decimal(lo) or
                       (Decimal(hi) == Decimal(lo) and (current['max_inclusive'] or row['min_inclusive']))))
            if exact or joined:
                current['source_offers'] = current['source_offers'] + source_rows(row)
                if hi is not None and (row['amount_max'] is None or Decimal(row['amount_max']) > Decimal(hi)):
                    current['amount_max'], current['max_inclusive'] = row['amount_max'], row['max_inclusive']
                elif hi == row['amount_max']:
                    current['max_inclusive'] = current['max_inclusive'] or row['max_inclusive']
            else:
                output.append(current)
                current = dict(row, source_offers=source_rows(row))
        if current is not None:
            output.append(current)
    for row in output:
        originals = source_rows(row)
        if len(originals) > 1:
            audit.append(dict(bank=row['bank'], currency=row['currency'], tenor=str(row['tenor_value'])+row['tenor_unit'],
                              product_id=row['product_id'], audience=row['audience'], rate_pct=row['rate_pct'],
                              amount_min=row['amount_min'], amount_max=row['amount_max'],
                              min_inclusive=row['min_inclusive'], max_inclusive=row['max_inclusive'],
                              input_rows=[r['input_row'] for r in originals]))
    return output, audit


def caption_identity(label):
    text = unicodedata.normalize('NFKC', str(label or '')).strip()
    # This suffix is display metadata, not an eligibility condition. Product
    # dates remain in raw detail and the newest caption is written back in full.
    text = re.sub(r'[;；]\s*公告期\s+.*$', '', text, flags=re.S)
    return re.sub(r'\s+', '', text).casefold()


def _audience(label):
    s = str(label or '').lower()
    if re.search(r'priority private|/\s*private\b', s): return 'private'
    if re.search(r'premier_elite|premier elite', s): return 'premier_elite'
    if re.search(r'premier_wealth|premier wealth', s): return 'premier_wealth'
    if re.search(r'premier_standard', s): return 'premier_standard'
    if re.search(r'priority banking|/\s*premier\b', s): return 'premier'
    if re.search(r'/\s*preferred\b|preferred', s): return 'preferred'
    if re.search(r'personal banking|/\s*personal\b', s): return 'personal'
    if re.search(r'citigold|private|premier', s): return 'special_unspecified'
    return None


def _legacy_amount(label):
    s = unicodedata.normalize('NFKC', str(label or '')).lower()
    # Old BOC captions embed obsolete rate headlines before the actual amount.
    s = re.sub(r'\d+(?:\.\d+)?\s*%', '', s)
    found = re.findall(r'(\d[\d,]*(?:\.\d+)?)\s*(million|mil|[km])?', s)
    values = [Decimal(n.replace(',', '')) * {'k': 1000, 'm': 1000000, 'mil': 1000000, 'million': 1000000}.get(unit, 1)
              for n, unit in found]
    if not values or len(values) > 2:
        return None
    if len(values) == 2:
        return values[0], values[1]
    if re.search(r'<(?![=>])|≤|以下|below', s):
        return None, values[0]
    return values[0], None


def reuse_product_row(offer, labels, caption, used=()):
    """Choose at most one old row; ambiguous legacy eligibility stays separate.

    The legacy fallback is deliberately confined to verified adapters with a
    known ordinary product. It links an old amount-only history row to its
    current product; it does not claim that historical eligibility was equal.
    """
    available = {r: label for r, label in labels.items() if r not in used}
    exact = sorted(r for r, label in available.items() if caption_identity(label) == caption_identity(caption))
    if exact:
        # Old pipelines could append the same full caption several times.
        # Reuse its first remaining row; all unassigned rows receive '-' for
        # the new date in the plan, while prior observations stay untouched.
        # Creating a third identical row would make every rerun grow forever.
        return exact[0], 'duplicate_caption_first_row' if len(exact)>1 else 'same_product_caption'
    bank = offer['bank']
    if bank not in {'BOC', 'CIMB', 'CITI', 'BEA', 'RHB', 'SBI', 'ICBC', 'HSBC', 'SCB'}:
        return None, 'new_product'
    target_lo = Decimal(offer['amount_min']) if offer['amount_min'] is not None else None
    target_hi = Decimal(offer['amount_max']) if offer['amount_max'] is not None else None
    matches = []
    for row, label in available.items():
        text = str(label or '')
        audience = _audience(text)
        if audience and audience != offer['audience']:
            continue
        if not audience and offer['audience'] not in {'personal', 'all'}:
            continue
        # Generated full captions encode channels/conditions that must not be
        # treated as an old shorthand solely because they share an amount.
        if '\n' in text and '/' in text:
            continue
        if bank == 'SCB' and audience != offer['audience']:
            continue
        if bank == 'CITI' and offer['product_id'] != 'citi-fx-conversion':
            continue
        if bank == 'CITI' and ('换汇' not in text and not re.fullmatch(r'\s*USD\s*50\s*[kK]\s*[-–]\s*5\s*(?:mil|million)\s*', text, re.I)):
            continue
        bounds = _legacy_amount(text)
        if not bounds:
            continue
        lo, hi = bounds
        # Same lower tier may gain a newly published upper cut-off. A newly
        # added higher tier never reuses that lower tier's historical row.
        same = ((lo == target_lo and lo is not None and (hi is None or hi == target_hi)) or
                (lo is None and hi is not None and hi == target_hi))
        if same:
            matches.append(row)
    if len(matches) == 1:
        return matches[0], 'legacy_product_amount'
    return None, 'ambiguous_legacy' if matches else 'new_product'

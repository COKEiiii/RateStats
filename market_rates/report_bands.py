"""Amount interval reconciliation for the report display, without editing quotes."""
from collections import defaultdict
from decimal import Decimal
import re


_NUMBER = r'[0-9][0-9,]*(?:\.[0-9]+)?'
_CURRENCY = r'USD|CNY|CNH|RMB|SGD|AUD|NZD|GBP|EUR|HKD|CAD|CHF'


def explicit_band(label, currency):
    """Accept only explicit deposit bounds, never numbers from other terms."""
    label = label.replace('>=', '≥').replace('<=', '≤')
    match = re.fullmatch(
        rf'(?:(?P<currency>{_CURRENCY})\s*(?P<equiv>等值)?\s*)?'
        rf'(?P<op>[<>≥≤])\s*(?P<number>{_NUMBER})'
        rf'(?:\s*且\s*(?P<upper>[<≤])\s*(?P<max>{_NUMBER}))?',
        label, re.I)
    if not match:
        return None
    unit = (match['currency'] or currency).upper()
    unit = {'CNH':'CNY','RMB':'CNY'}.get(unit, unit)
    number = Decimal(match['number'].replace(',', ''))
    op = match['op']
    if op in '<≤':
        if match['upper']:
            return None
        return unit, bool(match['equiv']), (None, number, False, op == '≤')
    upper = Decimal(match['max'].replace(',', '')) if match['max'] else None
    li, ui = op == '≥', match['upper'] == '≤'
    if upper is not None and (number > upper or (number == upper and not (li and ui))):
        return None
    return unit, bool(match['equiv']), (number, upper, li, ui)


def legacy_usd_range(label):
    """Legacy whole-dollar bracket labels, with both bounds specified."""
    match = re.fullmatch(
        rf'(?:USD\s+)?({_NUMBER})\s*([kK]?)\s*[-–]\s*({_NUMBER})\s*([kK]?)',
        label.strip())
    if not match:
        return None
    a, ak, b, bk = match.groups()
    lo = Decimal(a.replace(',', '')) * (1000 if ak else 1)
    hi = Decimal(b.replace(',', '')) * (1000 if bk else 1)
    return (lo, hi, True, False) if lo < hi else None


def whole_dollar_key(band):
    lo, hi, li, ui = band
    if hi is not None and ui and hi == int(hi) and int(hi) % 1000 == 999:
        hi, ui = hi + 1, False
    return lo, hi, li, ui


def contains(parent, child):
    lo, hi, li, ui = parent
    a, b, ai, bi = child
    return (
        (lo is None or (a is not None and (lo < a or (lo == a and (li or not ai)))))
        and (hi is None or (b is not None and (hi > b or (hi == b and (ui or not bi)))))
    )


def rate_key(rate):
    # Excel stores rates as binary doubles; ignore representation noise only.
    return rate.quantize(Decimal('0.000000000001'))


def combine_rates(records):
    values = defaultdict(list)
    for record in records:
        for col, rate in record['rates'].items():
            values[col].append(rate)
    if any(len({rate_key(rate) for rate in rates}) > 1 for rates in values.values()):
        return None
    return {col: rates[0] for col, rates in values.items()}


def partition(records):
    """Return disjoint, fully covered bands and contributing observations."""
    edges = sorted({n for record in records for n in record['band'][:2] if n is not None})
    atoms = []
    for i, edge in enumerate(edges):
        atoms.extend(((edges[i-1] if i else None, edge, False, False),
                      (edge, edge, True, True)))
    if edges:
        atoms.append((edges[-1], None, False, False))
    result = []
    for atom in atoms:
        covered = [record for record in records if contains(record['band'], atom)]
        if not covered:
            continue
        if result and result[-1][1] == covered:
            old = result[-1][0]
            if old[1] == atom[0] and (old[3] or atom[2]):
                result[-1] = ((old[0], atom[1], old[2], atom[3]), covered)
                continue
        result.append((atom, covered))
    return result


def band_text(unit, equivalent, band):
    lo, hi, li, ui = band
    limits = []
    if lo is not None:
        limits.append(('≥' if li else '>') + format(lo.normalize(), ',f'))
    if hi is not None:
        limits.append(('≤' if ui else '<') + format(hi.normalize(), ',f'))
    return unit + (' 等值 ' if equivalent else ' ') + ' 且'.join(limits)

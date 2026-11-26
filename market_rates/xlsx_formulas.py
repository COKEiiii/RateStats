"""Expand Excel shared formulas without changing cached values or other XML."""
from __future__ import annotations

M = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def expand_shared_formulas(root):
    """Resolve sheet-local shared groups before readers or editors use cells.

    Resolve all anchors first: XML order need not put an anchor first. Invalid
    groups fail explicitly rather than silently becoming an empty formula.
    Only the in-memory XML is changed; array/data-table formulas are untouched.
    """
    shared = []
    anchors = {}
    for cell in root.findall(M + 'sheetData/' + M + 'row/' + M + 'c'):
        formula = cell.find(M + 'f')
        if formula is None or formula.get('t') != 'shared':
            continue
        group = formula.get('si')
        if group is None:
            raise ValueError('Shared formula missing group: ' + cell.get('r', '?'))
        shared.append((cell, formula, group))
        if formula.text:
            if group in anchors:
                raise ValueError('Duplicate shared formula anchor: ' + group)
            anchors[group] = (cell.get('r'), formula.text, formula.get('ref'))
    if not shared:
        return
    # Already a project dependency; use its formula tokenizer, not regex, so
    # strings, sheet names, absolute references and row/column ranges survive.
    from openpyxl.formula.translate import Translator
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
    resolved = []
    translators = {}
    for cell, formula, group in shared:
        location = cell.get('r')
        if group not in anchors:
            raise ValueError('Missing shared formula anchor: ' + group + '/' + location)
        origin, text, refs = anchors[group]
        if refs:
            c1, r1, c2, r2 = range_boundaries(refs)
            row, column = coordinate_to_tuple(location)
            if not (r1 <= row <= r2 and c1 <= column <= c2):
                raise ValueError('Shared formula outside anchor range: ' + location)
        if group not in translators:
            translators[group] = Translator('=' + text, origin=origin)
        translated = translators[group].translate_formula(location)
        if not translated.startswith('=') or len(translated) < 2:
            raise ValueError('Empty shared formula: ' + location)
        resolved.append((formula, translated[1:]))
    # Apply only after every group resolves successfully, preserving caches,
    # styles and formula metadata unrelated to shared-formula compression.
    for formula, text in resolved:
        formula.text = text
        for attr in ('t', 'si', 'ref'):
            formula.attrib.pop(attr, None)

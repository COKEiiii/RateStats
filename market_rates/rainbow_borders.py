"""Restore bank-block boundaries in the SGD rainbow delivery copy."""
from copy import deepcopy
from pathlib import Path
import os
import tempfile
import xml.etree.ElementTree as ET

from .report_xml import Book, M, pos


def fix_sgd_bank_borders(path):
    path = Path(path)
    book = Book(path)
    sheet = book.sheets['SGD Promotional Rate']
    styles = book.xml('xl/styles.xml')
    borders, xfs = styles.find(M+'borders'), styles.find(M+'cellXfs')
    cache = {}

    def outline(node, edges):
        original = int(node.get('s', '0'))
        key = (original, edges)
        if key not in cache:
            xf = deepcopy(xfs[original])
            border = deepcopy(borders[int(xf.get('borderId', '0'))])
            for edge in edges:
                old = border.find(M+edge)
                if old is None:
                    old = ET.SubElement(border, M+edge)
                old.clear(); old.set('style', 'thin')
                ET.SubElement(old, M+'color', rgb='FF000000')
            xf.set('borderId', str(len(borders))); xf.set('applyBorder', '1')
            borders.append(border); xfs.append(xf)
            cache[key] = len(xfs)-1
        node.set('s', str(cache[key]))

    merged = {}
    for item in sheet.root.findall(M+'mergeCells/'+M+'mergeCell'):
        a, sep, b = item.get('ref').partition(':')
        if sep:
            r1, c1 = pos(a); r2, c2 = pos(b)
            if c1 == c2 and r1 >= 3:
                merged[r1, c1] = r2
    count = 0
    for (row, col), node in list(sheet.cells.items()):
        if row < 3 or (col-1) % 3 or not sheet.value(row, col):
            continue
        if sheet.value(row, col+1) is None:
            continue
        last = merged.get((row, col), row)
        outline(node, ('top', 'bottom'))
        if last != row:
            outline(sheet.cell(last, col), ('bottom',))
        count += 1
    borders.set('count', str(len(borders))); xfs.set('count', str(len(xfs)))
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.xlsx', delete=False) as f:
        temporary = f.name
    try:
        book.save(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return count

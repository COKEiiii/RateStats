"""Discover RHB SGD rows from the current PDF, retaining original-pixel proof.

The deposit-rates PDF includes SGD fixed deposits, FX call accounts and FX
fixed deposits.  Table number / page coordinates are not product identifiers.
"""
import hashlib
import re
from datetime import datetime, timezone

import pymupdf
from .evidence_capture import pdf_overview


TITLE = re.compile(r'SGD\s+Fixed\s+Deposit\s+Rates\s*\(%\s*p\.a\.\)', re.I)
DATE = re.compile(r'\b\d{1,2}-[A-Za-z]{3}-\d{2,4}\b')
TENOR = re.compile(r'^(\d+)-(?:mth|month)s?(\*)?$', re.I)
RATE = re.compile(r'^(?:\d+(?:\.\d+)?|[-–—])$')


def parse_sgd_grid(grid):
    """Find the printed SGD matrix; reject missing cells instead of shifting rates."""
    headers = []
    for index, row in enumerate(grid):
        cells = list(row)
        while cells and cells[-1] in (None, ''):
            cells.pop()
        if cells and str(cells[0]).strip() == 'Deposit Amount':
            if len(cells) < 3 or not all(TENOR.fullmatch(str(v).strip()) for v in cells[1:]):
                raise ValueError('RHB SGD tenor header changed or contains an unsupported tenor')
            headers.append((index, [str(v).strip() for v in cells]))
    if len(headers) != 1:
        raise ValueError('RHB SGD deposit-amount header is missing or ambiguous')
    start, header = headers[0]
    terms = [int(TENOR.fullmatch(v)[1]) for v in header[1:]]
    if len(terms) != len(set(terms)):
        raise ValueError('RHB SGD repeats a tenor column')
    rows, indices = [header], [start]
    for index in range(start + 1, len(grid)):
        physical = list(grid[index])
        if not physical or not str(physical[0] or '').strip().startswith('$'):
            break
        while physical and physical[-1] in (None, ''):
            physical.pop()
        if len(physical) != len(header) or any(v in (None, '') for v in physical):
            raise ValueError('RHB SGD rate row has missing cells or a changed column count')
        row = [str(v).strip() for v in physical]
        if not all(RATE.fullmatch(v) for v in row[1:]):
            raise ValueError('RHB SGD rate row contains unrecognized rate text')
        # An explicit minimum and optional upper limit must remain identifiable.
        if not re.fullmatch(r'\$[\d,]+\s+(?:to\s+\$[\d,]+|&\s+above)', row[0], re.I):
            raise ValueError('RHB SGD amount tier changed: ' + row[0])
        rows.append(row)
        indices.append(index)
    if len(rows) < 2:
        raise ValueError('RHB SGD matrix contains no quoted amount tiers')
    return rows, indices


def corporate_tenors(note, headers):
    """Read the actual footnote instead of unconditionally suppressing 1M/2M."""
    if not re.search(r'applicable\s+for\s+Commercial\s+and\s+Corporate\s+customers\s+only', note, re.I):
        raise ValueError('RHB SGD starred-tenor eligibility footnote is missing or changed')
    terms = sorted({int(v) for v in re.findall(r'(\d+)-(?:mth|month)', note, re.I)})
    starred = sorted(int(TENOR.fullmatch(v)[1]) for v in headers if v.endswith('*'))
    if terms != starred or not terms:
        raise ValueError('RHB SGD corporate-only note does not match the starred tenor columns')
    return terms


def _lines(page):
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            value = ''.join(span['text'] for span in line['spans']).strip()
            if value:
                yield value, pymupdf.Rect(line['bbox'])


def capture_sgd_pdf(batch, document, url):
    """Append one SGD section to BoardCapture using dynamically found PDF geometry."""
    candidates = []
    for page_index, page in enumerate(document):
        if not TITLE.search(page.get_text()):
            continue
        for table in page.find_tables().tables:
            raw = table.extract()
            if any(row and str(row[0]).strip() == 'Deposit Amount' for row in raw):
                rows, indices = parse_sgd_grid(raw)
                candidates.append((page_index, table, rows, indices))
    if len(candidates) != 1:
        raise ValueError('RHB PDF must contain exactly one identifiable SGD fixed-deposit matrix')
    page_index, table, rows, indices = candidates[0]
    page = document[page_index]
    lines = list(_lines(page))
    headings = [(s, r) for s, r in lines if TITLE.search(s)]
    dates = [(s, r) for s, r in lines if DATE.fullmatch(s)]
    corporate = [(s, r) for s, r in lines if 'Commercial' in s and 'Corporate' in s]
    below = [(s, r) for s, r in lines if re.search(r'balances\s+below', s, re.I)]
    if any(len(items) != 1 for items in [headings, dates, corporate, below]):
        raise ValueError('RHB SGD heading, publication date or eligibility notes missing/ambiguous')
    corporate_only = corporate_tenors(corporate[0][0], rows[0][1:])
    date_text = dates[0][0]
    source_date = datetime.strptime(date_text, '%d-%b-%Y' if len(date_text.rsplit('-', 1)[1]) == 4 else '%d-%b-%y').date().isoformat()
    key = 'sgd-p' + str(page_index + 1)
    metadata = dict(id=batch.prefix + key, bank='RHB', currency='SGD', rate_type='board',
                    url=url, ok=True, complete=True, captured_at=datetime.now(timezone.utc).isoformat(),
                    images=[], image_hashes={}, text=page.get_text())
    metadata['sha256'] = hashlib.sha256(metadata['text'].encode()).hexdigest()
    ids, evidence = [], []

    def tile(task_key, rect, expected, kind):
        # Include a small margin without assuming a fixed page size or position.
        rect = pymupdf.Rect(rect) + (-1, -1, 1, 1)
        rect &= page.rect
        name = pdf_overview(page,batch.ev,batch.prefix+'rhb')
        tid = batch.task(metadata, task_key, expected, name, kind)
        ids.append(tid)
        return batch.tasks[-1]

    for position, (row, index) in enumerate(zip(rows, indices)):
        # The PDF table detector may include a trailing empty cell reaching into
        # the wider FX table. Only the nonempty SGD cells define this row's width.
        physical = table.rows[index].cells[:len(row)]
        if any(cell is None for cell in physical):
            raise ValueError('RHB SGD physical cell boundaries are missing')
        rect = pymupdf.Rect(physical[0])
        for cell in physical[1:]:
            rect |= pymupdf.Rect(cell)
        task = tile(key + '-row-' + str(position), rect, [row], 'grid')
        task['strict_row_layout'] = True
    for label, items in [('heading', headings), ('date', dates), ('corporate', corporate), ('below-minimum', below)]:
        value, rect = items[0]
        tile(key + '-' + label, rect, value, 'text')
        evidence.append(dict(page_id=metadata['id'], quote=value,
                             locator=batch.prefix + key + '-' + label))
    evidence.append(dict(page_id=metadata['id'], quote='Rates as at: ' + source_date,
                         locator=batch.prefix + key + '/source-date'))
    section = dict(id=batch.prefix + 'sgd-board', bank='RHB', currency='SGD', layout='rhb-sgd',
                   product_id='rhb-sgd-board', table=0, rows=rows, task_ids=ids,
                   page_id=metadata['id'], rate_basis='annual_nominal', source_date=source_date,
                   corporate_only_tenors=corporate_only, extra_evidence=evidence)
    batch.pages.append(metadata)
    batch.sections.append(section)
    return section

"""Small, lossless OOXML editor for the report formatter.

Untouched ZIP members stay byte-for-byte intact. Unlike a spreadsheet object
model round-trip this retains Excel's extension records and existing styles.
This module does not evaluate Excel formulas: callers must use saved caches.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from .xlsx_formulas import expand_shared_formulas

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
M = '{' + NS + '}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
P = '{http://schemas.openxmlformats.org/package/2006/relationships}'
XML = '{http://www.w3.org/XML/1998/namespace}'


def col(n):
    result = ''
    while n:
        n, digit = divmod(n - 1, 26)
        result = chr(65 + digit) + result
    return result


def pos(address):
    match = re.fullmatch(r'\$?([A-Z]+)\$?(\d+)', address)
    if not match:
        raise ValueError(f'Unsupported cell address: {address}')
    n = 0
    for char in match[1]:
        n = n * 26 + ord(char) - 64
    return int(match[2]), n


def address(row, column):
    return f'{col(column)}{row}'


def numeric(value):
    return isinstance(value, Decimal) and value.is_finite()


def namespaces(data):
    return dict(pair for _, pair in ET.iterparse(io.BytesIO(data), events=['start-ns']))


def serialize(root, original=b''):
    # mc:Ignorable contains prefix names as text; ElementTree otherwise removes
    # their declarations when a namespace is not used by any element/attribute.
    ns = namespaces(original) if original else {'': NS, 'r': R[1:-1]}
    for prefix, uri in ns.items():
        if not re.fullmatch(r'ns\d+', prefix):
            ET.register_namespace(prefix, uri)
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    end = data.find(b'>', data.find(b'?>') + 2)
    head = data[:end]
    extra = b''
    for prefix, uri in ns.items():
        label = ('xmlns:' + prefix if prefix else 'xmlns').encode()
        if label + b'=' not in head:
            extra += b' ' + label + b'="' + uri.encode() + b'"'
    insert = end - 1 if data[end-1:end] == b'/' else end
    return data[:insert] + extra + data[insert:]


def local(tag):
    return tag.rsplit('}', 1)[-1]


def remove_column_icons(root, column):
    """Remove quote-column arrows while preserving other CF rules/ranges."""
    changed_parents = set()
    def retained(refs):
        result = []
        for ref in refs.split():
            match = re.fullmatch(r'\$?([A-Z]+)(\$?\d+)?(?::\$?([A-Z]+)(\$?\d+)?)?', ref)
            if not match:
                result.append(ref)
                continue
            first = pos(match[1] + '1')[1]
            last = pos((match[3] or match[1]) + '1')[1]
            if not first <= column <= last:
                result.append(ref)
                continue
            r1, r2 = match[2] or '', match[4] or match[2] or ''
            for left, right in ((first, column - 1), (column + 1, last)):
                if left <= right:
                    a, b = col(left) + r1, col(right) + r2
                    result.append(a if a == b else a + ':' + b)
        return ' '.join(result)

    for parent in list(root.iter()):
        for cf in list(parent):
            if local(cf.tag) != 'conditionalFormatting':
                continue
            icons = [r for r in cf if local(r.tag) == 'cfRule' and r.get('type') == 'iconSet']
            if not icons:
                continue
            sqref = next((e for e in cf if local(e.tag) == 'sqref'), None)
            refs = cf.get('sqref', sqref.text if sqref is not None else '')
            keep = retained(refs)
            if keep == refs:
                continue
            changed_parents.add(parent)
            other_rules = [r for r in cf if local(r.tag) == 'cfRule' and r not in icons]
            if other_rules:
                icon_cf = deepcopy(cf)
                for rule in list(icon_cf):
                    if local(rule.tag) == 'cfRule' and rule.get('type') != 'iconSet':
                        icon_cf.remove(rule)
                for rule in icons:
                    cf.remove(rule)
                if keep:
                    parent.insert(list(parent).index(cf) + 1, icon_cf)
                cf = icon_cf
            elif not keep:
                parent.remove(cf)
            if keep:
                if 'sqref' in cf.attrib:
                    cf.set('sqref', keep)
                else:
                    next(e for e in cf if local(e.tag) == 'sqref').text = keep
    parents = {child: parent for parent in root.iter() for child in parent}
    for element in changed_parents:
        while not list(element) and local(element.tag) in ('conditionalFormattings', 'ext', 'extLst'):
            parent = parents.get(element)
            if parent is None:
                break
            parent.remove(element)
            element = parent


class Sheet:
    def __init__(self, book, name, path, node):
        self.book, self.name, self.path, self.node = book, name, path, node
        self.root = book.xml(path)
        expand_shared_formulas(self.root)
        self.data = self.root.find(M + 'sheetData')
        self.refresh()

    def refresh(self):
        self.rows = {int(r.attrib['r']): r for r in self.data}
        self.cells = {pos(c.attrib['r']): c for r in self.data for c in r if local(c.tag) == 'c'}

    @property
    def maxrow(self):
        return max(self.rows, default=1)

    @property
    def maxcol(self):
        return max((c for r, c in self.cells), default=1)

    def value(self, row, column):
        c = self.cells.get((row, column))
        if c is None:
            return None
        typ = c.get('t')
        raw = c.find(M + 'v')
        text = raw.text if raw is not None else None
        if typ == 's' and text is not None:
            return self.book.strings[int(text)]
        if typ == 'inlineStr':
            return ''.join(t.text or '' for t in c.findall('.//' + M + 't'))
        if text is None:
            return None
        if typ in ('str', 'e', 'd'):
            return text
        try:
            return Decimal(text)
        except InvalidOperation:
            return text

    def cell(self, row, column):
        if (row, column) not in self.cells:
            if row not in self.rows:
                self.rows[row] = ET.Element(M + 'row', r=str(row))
            self.cells[row, column] = ET.Element(M + 'c', r=address(row, column))
        return self.cells[row, column]

    def put(self, row, column, value, style=None, formula=None):
        c = self.cell(row, column)
        for child in list(c):
            if local(child.tag) in ('v', 'is', 'f'):
                c.remove(child)
        c.attrib.pop('t', None)
        if style is not None:
            c.set('s', str(style))
        if formula is not None:
            ET.SubElement(c, M + 'f').text = formula.lstrip('=')
        if value is None:
            return c
        if isinstance(value, (int, float, Decimal)):
            ET.SubElement(c, M + 'v').text = str(value)
        elif formula is not None:
            c.set('t', 'str')
            ET.SubElement(c, M + 'v').text = str(value)
        else:
            c.set('t', 'inlineStr')
            t = ET.SubElement(ET.SubElement(c, M + 'is'), M + 't')
            t.set(XML + 'space', 'preserve')
            t.text = str(value)
        return c

    def height(self, row, value):
        self.cell(row, 1)
        self.rows[row].set('ht', str(value))
        self.rows[row].set('customHeight', '1')

    def remove_icons(self):
        for cf in list(self.root.findall(M + 'conditionalFormatting')):
            for rule in list(cf):
                if rule.get('type') == 'iconSet':
                    cf.remove(rule)
            if not list(cf):
                self.root.remove(cf)
        for parent in self.root.iter():
            for child in list(parent):
                if local(child.tag) == 'conditionalFormattings':
                    # Excel x14 icon rules reference the old layout. Preserve
                    # unrelated extended rules; drop only extended icon rules.
                    for cf in list(child):
                        for rule in list(cf):
                            if local(rule.tag) == 'cfRule' and rule.get('type') == 'iconSet':
                                cf.remove(rule)
                        if not any(local(x.tag) == 'cfRule' for x in cf):
                            child.remove(cf)
                    if not list(child):
                        parent.remove(child)

    def insert_element(self, element):
        order = ('sheetPr dimension sheetViews sheetFormatPr cols sheetData sheetCalcPr sheetProtection '
                 'protectedRanges scenarios autoFilter sortState dataConsolidate customSheetViews '
                 'mergeCells phoneticPr conditionalFormatting dataValidations hyperlinks printOptions '
                 'pageMargins pageSetup headerFooter rowBreaks colBreaks customProperties cellWatches '
                 'ignoredErrors smartTags drawing legacyDrawing legacyDrawingHF picture oleObjects '
                 'controls webPublishItems tableParts extLst').split()
        rank = order.index(local(element.tag))
        for i, child in enumerate(self.root):
            if local(child.tag) in order and order.index(local(child.tag)) > rank:
                self.root.insert(i, element)
                return
        self.root.append(element)

    def delta_rule(self, refs):
        if not refs:
            return
        cf = ET.Element(M + 'conditionalFormatting', sqref=' '.join(refs))
        rule = ET.SubElement(cf, M + 'cfRule', type='iconSet', priority='1')
        icons = ET.SubElement(rule, M + 'iconSet', iconSet='3Arrows', showValue='1', reverse='0')
        ET.SubElement(icons, M + 'cfvo', type='num', val='0')
        ET.SubElement(icons, M + 'cfvo', type='num', val='0', gte='1')
        ET.SubElement(icons, M + 'cfvo', type='num', val='0', gte='0')
        for other in self.root.findall(M + 'conditionalFormatting/' + M + 'cfRule'):
            other.set('priority', str(int(other.get('priority', '1')) + 1))
        self.insert_element(cf)

    def remap(self, rowmap=None, colmap=None):
        rowmap = rowmap or {r:r for r in range(1, self.maxrow + 1)}
        colmap = colmap or (lambda c:c)
        def ref(text):
            def one(m):
                r, c = pos(m[0])
                return address(rowmap.get(r, r), colmap(c))
            return re.sub(r'\$?[A-Z]+\$?\d+', one, text)
        newrows, newcells = {}, {}
        for r, element in sorted(self.rows.items()):
            nr = rowmap.get(r, r)
            if nr not in newrows:
                newrows[nr] = deepcopy(element)
                newrows[nr].set('r', str(nr))
                newrows[nr].attrib.pop('spans', None)
        for (r, c), element in sorted(self.cells.items()):
            nr, nc = rowmap.get(r, r), colmap(c)
            if (nr, nc) in newcells:
                if self.value(r, c) is not None:
                    raise ValueError(f'Unresolved cell collision: {self.name}!{address(r,c)}')
                continue
            newcells[nr, nc] = element
            element.set('r', address(nr, nc))
        self.rows, self.cells = newrows, newcells
        for node in self.root.iter():
            if local(node.tag) in ('c', 'row'):
                continue
            for attr in ('ref', 'sqref', 'activeCell', 'topLeftCell'):
                if attr in node.attrib:
                    node.set(attr, ref(node.get(attr)))
            if local(node.tag) in ('formula', 'formula1', 'formula2') and node.text:
                node.text = ref(node.text)
            if local(node.tag) == 'pane':
                if node.get('ySplit') and float(node.get('ySplit')).is_integer():
                    node.set('ySplit', str(rowmap.get(int(float(node.get('ySplit'))) + 1, 1) - 1))
                if node.get('xSplit') and float(node.get('xSplit')).is_integer():
                    node.set('xSplit', str(colmap(int(float(node.get('xSplit'))) + 1) - 1))
        merges = self.root.find(M + 'mergeCells')
        if merges is not None:
            seen = set()
            for merge in list(merges):
                a, _, b = merge.get('ref').partition(':')
                if a == b or merge.get('ref') in seen or (b and pos(a)[0] > pos(b)[0]):
                    merges.remove(merge)
                else:
                    seen.add(merge.get('ref'))
            merges.set('count', str(len(merges)))
        cols = self.root.find(M + 'cols')
        if cols is not None:
            for c in cols:
                c.set('min', str(colmap(int(c.get('min')))))
                c.set('max', str(colmap(int(c.get('max')))))
        # Comments and legacy note anchors are sheet-local package parts.
        relpath = posixpath.join(posixpath.dirname(self.path), '_rels', posixpath.basename(self.path)+'.rels')
        if relpath in self.book.parts:
            for relationship in self.book.xml(relpath):
                if relationship.get('TargetMode') == 'External':
                    continue
                target = self.book.resolve(self.path, relationship.get('Target'))
                typ = relationship.get('Type', '').rsplit('/', 1)[-1]
                if typ in ('comments', 'vmlDrawing'):
                    root = self.book.xml(target)
                    for node in root.iter():
                        if 'ref' in node.attrib:
                            node.set('ref', ref(node.get('ref')))
                        if local(node.tag) == 'Row' and node.text:
                            node.text = str(rowmap.get(int(node.text)+1, int(node.text)+1)-1)
                        if local(node.tag) == 'Column' and node.text:
                            node.text = str(colmap(int(node.text)+1)-1)
                        if local(node.tag) == 'Anchor' and node.text:
                            values = [int(x.strip()) for x in node.text.split(',')]
                            for i in (0, 4):
                                values[i] = colmap(values[i]+1)-1
                            for i in (2, 6):
                                values[i] = rowmap.get(values[i]+1, values[i]+1)-1
                            node.text = ', '.join(map(str, values))
                elif typ in ('drawing', 'table', 'pivotTable'):
                    raise ValueError(f'{self.name}: structural editing of {typ} is not supported')
        return ref

    def sync(self):
        self.data[:] = []
        byrow = {}
        for (r, c), element in sorted(self.cells.items()):
            byrow.setdefault(r, []).append(element)
        for r, row in sorted(self.rows.items()):
            row[:] = [x for x in row if local(x.tag) != 'c']
            row.extend(byrow.get(r, []))
            self.data.append(row)
        dim = self.root.find(M + 'dimension')
        if dim is not None:
            dim.set('ref', f'A1:{address(self.maxrow,self.maxcol)}')


class Book:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(path) as archive:
            self.parts = {info.filename:archive.read(info) for info in archive.infolist()}
        self.roots = {}
        self.strings = []
        if 'xl/sharedStrings.xml' in self.parts:
            self.strings = [''.join(t.text or '' for t in si.iter(M+'t'))
                            for si in self.xml('xl/sharedStrings.xml')]
        self.workbook = self.xml('xl/workbook.xml')
        self.rels = self.xml('xl/_rels/workbook.xml.rels')
        rels = {r.get('Id'):r.get('Target') for r in self.rels}
        self.sheets = {}
        for node in self.workbook.find(M+'sheets'):
            path = self.resolve('xl/workbook.xml', rels[node.get(R+'id')])
            self.sheets[node.get('name')] = Sheet(self, node.get('name'), path, node)

    @staticmethod
    def resolve(owner, target):
        return target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(owner),target))

    def xml(self, path):
        if path not in self.roots:
            self.roots[path] = ET.fromstring(self.parts[path])
        return self.roots[path]

    def add_sheet(self, name):
        """Append a plain worksheet without round-tripping unrelated OOXML."""
        if name in self.sheets or not name or len(name)>31:
            raise ValueError('Invalid or duplicate worksheet name: '+name)
        nodes=self.workbook.find(M+'sheets')
        number=max([0]+[int(n.get('sheetId')) for n in nodes])+1
        while f'xl/worksheets/sheet{number}.xml' in self.parts:number+=1
        path=f'xl/worksheets/sheet{number}.xml'
        used={n.get('Id') for n in self.rels};rid=f'rIdSheet{number}'
        while rid in used:rid+='x'
        node=ET.SubElement(nodes,M+'sheet',name=name,sheetId=str(number),attrib={R+'id':rid})
        ET.SubElement(self.rels,P+'Relationship',Id=rid,Type=R[1:-1]+'/worksheet',Target=f'worksheets/sheet{number}.xml')
        ET.SubElement(self.xml('[Content_Types].xml'),'{http://schemas.openxmlformats.org/package/2006/content-types}Override',PartName='/'+path,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
        root=ET.Element(M+'worksheet');ET.SubElement(root,M+'sheetData')
        self.parts[path]=ET.tostring(root);self.roots[path]=root
        sheet=Sheet(self,name,path,node);self.sheets[name]=sheet
        return sheet

    def drop_sheet(self, name):
        sheet = self.sheets.pop(name)
        self.workbook.find(M+'sheets').remove(sheet.node)
        for r in list(self.rels):
            if r.get('Id') == sheet.node.get(R+'id'):
                self.rels.remove(r)
        self.parts.pop(sheet.path, None)
        self.roots.pop(sheet.path, None)
        relpath = posixpath.join(posixpath.dirname(sheet.path),'_rels',posixpath.basename(sheet.path)+'.rels')
        self.parts.pop(relpath, None)
        self.roots.pop(relpath, None)

    def detach_external(self, removed):
        for r in list(self.rels):
            if r.get('Type','').endswith(('/externalLink','/calcChain')):
                self.rels.remove(r)
        for child in list(self.workbook):
            if local(child.tag) == 'externalReferences':
                self.workbook.remove(child)
        for name in list(self.parts):
            if name.startswith('xl/externalLinks/') or name == 'xl/calcChain.xml':
                self.parts.pop(name, None)
                self.roots.pop(name, None)
        names = self.workbook.find(M+'definedNames')
        if names is not None:
            for node in list(names):
                if '[' in (node.text or '') or any(name in (node.text or '') for name in removed):
                    names.remove(node)
        types = self.xml('[Content_Types].xml')
        for entry in list(types):
            part = entry.get('PartName','').lstrip('/')
            if part and part not in self.parts:
                types.remove(entry)
        calc = self.workbook.find(M+'calcPr')
        if calc is None:
            calc = ET.SubElement(self.workbook, M+'calcPr')
        calc.set('calcMode','auto')
        calc.set('fullCalcOnLoad','1')

    def save(self, path):
        for s in self.sheets.values():
            s.sync()
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in self.parts.items():
                archive.writestr(name, serialize(self.roots[name],data) if name in self.roots else data)


class StyleMap:
    def __init__(self, target, template):
        self.root = target.xml('xl/styles.xml')
        self.source = template.xml('xl/styles.xml')
        self.cache = {}
        self.custom_cache = {}
        self.resources = {}

    def import_resource(self, collection, index):
        key = collection, index
        if key not in self.resources:
            dst, src = self.root.find(M+collection), self.source.find(M+collection)
            node = deepcopy(src[index])
            raw = ET.tostring(node)
            match = next((i for i,v in enumerate(dst) if ET.tostring(v)==raw), None)
            if match is None:
                match = len(dst)
                dst.append(node)
                dst.set('count',str(len(dst)))
            self.resources[key] = match
        return self.resources[key]

    def numfmt(self, number):
        if number < 164:
            return number
        node = next(x for x in self.source.find(M+'numFmts') if int(x.get('numFmtId'))==number)
        return self.format_id(node.get('formatCode'))

    def format_id(self, code):
        formats = self.root.find(M+'numFmts')
        if formats is None:
            formats = ET.Element(M+'numFmts',count='0')
            self.root.insert(0,formats)
        match = next((int(x.get('numFmtId')) for x in formats if x.get('formatCode')==code),None)
        if match is not None:
            return match
        number = max([163]+[int(x.get('numFmtId')) for x in formats])+1
        ET.SubElement(formats,M+'numFmt',numFmtId=str(number),formatCode=code)
        formats.set('count',str(len(formats)))
        return number

    def get(self, source_id):
        source_id = int(source_id or 0)
        if source_id not in self.cache:
            xf = deepcopy(self.source.find(M+'cellXfs')[source_id])
            for attr, collection in [('fontId','fonts'),('fillId','fills'),('borderId','borders')]:
                xf.set(attr,str(self.import_resource(collection,int(xf.get(attr,'0')))))
            xf.set('numFmtId',str(self.numfmt(int(xf.get('numFmtId','0')))))
            xf.set('xfId','0')
            dst = self.root.find(M+'cellXfs')
            self.cache[source_id] = len(dst)
            dst.append(xf)
            dst.set('count',str(len(dst)))
        return self.cache[source_id]

    def customize(self, style, *, format_code=None, wrap=None, alignment=None):
        key = style,format_code,wrap,alignment
        if key not in self.custom_cache:
            dst = self.root.find(M+'cellXfs')
            xf = deepcopy(dst[int(style)])
            if format_code is not None:
                xf.set('numFmtId',str(self.format_id(format_code)))
                xf.set('applyNumberFormat','1')
            if wrap is not None or alignment:
                a = xf.find(M+'alignment')
                if a is None:
                    a = ET.SubElement(xf,M+'alignment')
                a.set('vertical','center')
                if wrap is not None:
                    a.set('wrapText','1' if wrap else '0')
                if alignment:
                    a.set('horizontal',alignment)
                xf.set('applyAlignment','1')
            self.custom_cache[key] = len(dst)
            dst.append(xf)
            dst.set('count',str(len(dst)))
        return self.custom_cache[key]

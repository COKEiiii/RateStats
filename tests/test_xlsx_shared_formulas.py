"""Excel-native shared formula compression must not hide formulas from checks."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from market_rates.xlsx_formulas import expand_shared_formulas, M
from market_rates.xlsx_read import read_xlsx
from market_rates.report_xml import Book, R, P, serialize


def sheet(cells):
    root = ET.Element(M + 'worksheet')
    data = ET.SubElement(root, M + 'sheetData')
    for address, text, attributes, cached in cells:
        row = ET.SubElement(data, M + 'row', r=''.join(filter(str.isdigit, address)))
        cell = ET.SubElement(row, M + 'c', r=address, s='0', t='str')
        ET.SubElement(cell, M + 'f', **attributes).text = text
        if cached is not None:
            ET.SubElement(cell, M + 'v').text = cached
    return root


def shared(group='0', **kwargs):
    return dict(t='shared', si=group, **kwargs)


class SharedFormulaTests(unittest.TestCase):
    def test_translation_handles_relative_absolute_ranges_and_strings(self):
        root = sheet([
            ('D5', None, shared(), '0'),  # follower precedes anchor in XML
            ('C4', 'SUM(A4,$A4,A$1,$A$1,\'中文 表\'!B4,A4:B5,4:4,A:A)+IF(A4="A4",1,0)',
             shared(ref='C4:D5', ca='1'), '-'),
        ])
        expand_shared_formulas(root)
        formulas = root.findall('.//' + M + 'f')
        self.assertEqual(formulas[0].text,
            'SUM(B5,$A5,B$1,$A$1,\'中文 表\'!C5,B5:C6,5:5,B:B)+IF(B5="A4",1,0)')
        self.assertEqual(formulas[1].attrib, {'ca': '1'})
        self.assertEqual([v.text for v in root.findall('.//' + M + 'v')], ['0', '-'])
        before = ET.tostring(root)
        expand_shared_formulas(root)
        self.assertEqual(ET.tostring(root), before)

    def test_real_delta_formula_translates_index_row_ranges(self):
        root = sheet([
            ('IT4', 'IF(COUNT(INDEX(4:4,1,4),INDEX(4:4,1,5))=2,ROUND(INDEX(4:4,1,4)-INDEX(4:4,1,5),8),"-")', shared(ref='IT4:IT42'), '-'),
            ('IT5', None, shared(), '-'),
        ])
        expand_shared_formulas(root)
        self.assertEqual(root.findall('.//' + M + 'f')[1].text,
            'IF(COUNT(INDEX(5:5,1,4),INDEX(5:5,1,5))=2,ROUND(INDEX(5:5,1,4)-INDEX(5:5,1,5),8),"-")')

    def test_invalid_shared_groups_fail_without_partial_changes(self):
        cases = [
            [('C4', None, shared(), '-')],
            [('C4', 'A4', shared(), '0'), ('C5', 'A5', shared(), '0')],
            [('C4', 'A4', shared(ref='C4:C5'), '0'), ('C6', None, shared(), None)],
            [('C4', 'A4', {'t': 'shared'}, '0')],
        ]
        for cells in cases:
            with self.subTest(cells=cells):
                root = sheet(cells)
                before = ET.tostring(root)
                with self.assertRaises(ValueError):
                    expand_shared_formulas(root)
                self.assertEqual(ET.tostring(root), before)

    def test_other_formula_kinds_and_metadata_are_untouched(self):
        root = sheet([('A1', 'SUM(B1:B3)', {'t': 'array', 'ref': 'A1:A3', 'aca': '1'}, '0')])
        before = ET.tostring(root)
        expand_shared_formulas(root)
        self.assertEqual(ET.tostring(root), before)

    def test_reader_and_editor_share_resolution_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / '含 空格.xlsx'
            output = Path(temp) / 'edited.xlsx'
            wb = ET.Element(M + 'workbook')
            sheets = ET.SubElement(wb, M + 'sheets')
            rels = ET.Element(P + 'Relationships')
            with zipfile.ZipFile(source, 'w') as archive:
                for i, name in enumerate(('中文 表', 'other'), 1):
                    ET.SubElement(sheets, M + 'sheet', name=name, sheetId=str(i), attrib={R+'id': 'r'+str(i)})
                    ET.SubElement(rels, P+'Relationship', Id='r'+str(i), Target=f'worksheets/sheet{i}.xml')
                    root = sheet([
                        ('C4', f'A4+{i}', shared(ref='C4:C6'), '0'),
                        ('C5', None, shared(), '-'),
                        ('C6', None, shared(), None),
                    ])
                    archive.writestr(f'xl/worksheets/sheet{i}.xml', serialize(root))
                archive.writestr('xl/workbook.xml', serialize(wb))
                archive.writestr('xl/_rels/workbook.xml.rels', serialize(rels))
                archive.writestr('custom/keep.bin', b'untouched')
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(read_xlsx(source)['中文 表'], {'C4': '0', 'C5': '-', 'C6': None})
            self.assertEqual(read_xlsx(source, formulas=True)['中文 表']['C5'], '=A5+1')
            self.assertEqual(read_xlsx(source, formulas=True)['other']['C5'], '=A5+2')
            book = Book(source)
            target = book.sheets['中文 表']
            self.assertEqual(target.cells[5, 3].findtext(M+'f'), 'A5+1')
            # Deleting an anchor must not orphan remaining followers on save.
            target.put(4, 3, 10)
            book.save(output)
            self.assertEqual(read_xlsx(output, formulas=True)['中文 表']['C5'], '=A5+1')
            self.assertEqual(read_xlsx(output)['中文 表']['C5'], '-')
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('custom/keep.bin'), b'untouched')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()

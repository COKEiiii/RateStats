"""Premier column defaults must not draw borders below a rebuilt shorter table."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import xml.etree.ElementTree as ET

from market_rates.report_xml import Book, M, StyleMap
from market_rates.report_format import format_premier
from tests.test_report_format import fixture


class PremierEmptyStyleTests(TestCase):
    def test_drop_only_column_defaults_keep_blank_rate_cells_and_hidden_flags(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.xlsx';fixture(path,date(2026,10,7))
            book,reference=Book(path),Book(path)
            def table(owner):
                sheet=owner.sheets['SGD促销'];sheet.name='Premier Rates';sheet.cells={};sheet.rows={}
                for merge in sheet.root.findall(M+'mergeCells'):sheet.root.remove(merge)
                for rc,value in {(1,1):'新元 2026-10-08',(2,1):'Bank',(2,2):'准入门槛',
                                 (2,3):'Minimum Amount',(2,4):'1M',(2,5):'3M',(2,6):'其他',
                                 (3,1):'HSBC',(3,2):'Premier',(3,3):'SGD ≥30,000',
                                 (3,5):Decimal('.02'),(3,6):'须新资金',(4,1):'本期未列入：旧报价已移除'}.items():
                    sheet.put(*rc,value,'0')
                sheet.put(3,4,None,'0') # A real table's unquoted tenor keeps its grid.
                return sheet
            current=table(book);sample=table(reference)
            stylesheet=reference.xml('xl/styles.xml');borders=stylesheet.find(M+'borders')
            border=ET.SubElement(borders,M+'border');ET.SubElement(border,M+'bottom',style='thin')
            border_index=len(borders)-1;borders.set('count',str(len(borders)))
            xfs=stylesheet.find(M+'cellXfs');border_style=deepcopy(xfs[0]);border_style.set('borderId',str(border_index))
            xfs.append(border_style);xfs.set('count',str(len(xfs)));style_id=str(len(xfs)-1)
            for cell in sample.cells.values():cell.set('s',style_id)
            for column in sample.root.findall(M+'cols/'+M+'col'):
                column.set('style',style_id);column.set('hidden','1')
            before={rc:current.value(*rc) for rc in current.cells}
            format_premier(current,sample,StyleMap(book,reference))
            columns=current.root.findall(M+'cols/'+M+'col')
            self.assertTrue(columns)
            self.assertTrue(all('style' not in column.attrib for column in columns))
            # Only 3M (E3) has a current numeric quote. Its column must be
            # visible; the blank 1M column and the other reference columns
            # retain their original hidden flags.
            self.assertEqual({int(column.get('min')):column.get('hidden') for column in columns if int(column.get('max'))<=6},
                             {1:'1',2:'1',3:'1',4:'1',5:'0',6:'1'})
            self.assertIsNone(current.value(3,4))
            xf=book.xml('xl/styles.xml').find(M+'cellXfs')[int(current.cells[3,4].get('s'))]
            retained_border=book.xml('xl/styles.xml').find(M+'borders')[int(xf.get('borderId'))]
            self.assertEqual(retained_border.find(M+'bottom').get('style'),'thin')
            for rc,value in before.items():self.assertEqual(current.value(*rc),value)
            self.assertEqual(current.maxrow,4)

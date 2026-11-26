"""Reference column styles must import resources, not reuse workbook-local IDs."""
from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import xml.etree.ElementTree as ET

from market_rates.report_xml import Book,M,StyleMap
from market_rates.report_format import copy_layout
from tests.test_report_format import fixture


class ColumnStyleMappingTests(TestCase):
    def test_default_column_style_matches_reference_resources_and_attributes(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'fixture.xlsx';fixture(path,date(2026,10,7))
            target,reference=Book(path),Book(path)
            source_styles=reference.xml('xl/styles.xml')
            target_styles=target.xml('xl/styles.xml')
            # The same numeric style ID intentionally means different borders
            # and number formats in the two independent workbooks.
            target_borders=target_styles.find(M+'borders')
            bordered=ET.SubElement(target_borders,M+'border')
            ET.SubElement(bordered,M+'bottom',style='thin')
            target_borders.set('count',str(len(target_borders)))
            wrong=deepcopy(target_styles.find(M+'cellXfs')[0]);wrong.set('borderId',str(len(target_borders)-1))
            target_styles.find(M+'cellXfs').append(wrong)
            target_styles.find(M+'cellXfs').set('count',str(len(target_styles.find(M+'cellXfs'))))

            fonts=source_styles.find(M+'fonts');font=deepcopy(fonts[0])
            ET.SubElement(font,M+'color',rgb='FF234567');fonts.append(font);fonts.set('count',str(len(fonts)))
            fills=source_styles.find(M+'fills');fill=ET.SubElement(fills,M+'fill')
            ET.SubElement(ET.SubElement(fill,M+'patternFill',patternType='solid'),M+'fgColor',rgb='FFEEDDCC')
            fills.set('count',str(len(fills)))
            formats=source_styles.find(M+'numFmts');ET.SubElement(formats,M+'numFmt',numFmtId='200',formatCode='0.000%')
            formats.set('count',str(len(formats)))
            expected=deepcopy(source_styles.find(M+'cellXfs')[0])
            expected.set('fontId',str(len(fonts)-1));expected.set('fillId',str(len(fills)-1));expected.set('numFmtId','200')
            expected.set('applyNumberFormat','1');expected.set('applyAlignment','1')
            ET.SubElement(expected,M+'alignment',wrapText='1',horizontal='right')
            source_styles.find(M+'cellXfs').append(expected)
            source_styles.find(M+'cellXfs').set('count',str(len(source_styles.find(M+'cellXfs'))))
            ref=reference.sheets['SGD促销'];sheet=target.sheets['SGD促销']
            for column in ref.root.findall(M+'cols/'+M+'col'):
                column.set('style','1');column.set('hidden','1')
            before={rc:sheet.value(*rc) for rc in sheet.cells}
            copy_layout(sheet,ref,StyleMap(target,reference))
            column=next(c for c in sheet.root.findall(M+'cols/'+M+'col') if c.get('min')=='3')
            actual=target_styles.find(M+'cellXfs')[int(column.get('style'))]
            self.assertEqual(column.get('hidden'),'1')
            self.assertEqual(column.get('width'),'15')
            for attr,collection in [('fontId','fonts'),('fillId','fills'),('borderId','borders')]:
                a=target_styles.find(M+collection)[int(actual.get(attr))]
                e=source_styles.find(M+collection)[int(expected.get(attr))]
                self.assertEqual(ET.tostring(a),ET.tostring(e),collection)
            actual_format=next(n.get('formatCode') for n in target_styles.find(M+'numFmts') if n.get('numFmtId')==actual.get('numFmtId'))
            self.assertEqual(actual_format,'0.000%')
            ignored={'fontId','fillId','borderId','numFmtId'}
            self.assertEqual({k:v for k,v in actual.attrib.items() if k not in ignored},
                             {k:v for k,v in expected.attrib.items() if k not in ignored})
            self.assertEqual([ET.tostring(c) for c in actual],[ET.tostring(c) for c in expected])
            for rc,value in before.items():self.assertEqual(sheet.value(*rc),value)

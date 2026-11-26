import unittest
import xml.etree.ElementTree as ET

from market_rates.report_xml import M, NS, namespaces, remove_column_icons, serialize


class QuoteIconTests(unittest.TestCase):
    def test_namespace_and_history_rules_survive_quote_arrow_removal(self):
        raw = (f'<worksheet xmlns="{NS}" '
               'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
               'xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision" '
               'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" '
               'mc:Ignorable="xr x14ac"><sheetData><row r="4"><c r="C4">'
               '<f>MAX(A4:B4)</f><v>0.02</v></c></row></sheetData>'
               '<conditionalFormatting sqref="C4:E4 A8">'
               '<cfRule type="iconSet" priority="1"><iconSet iconSet="3Arrows"/></cfRule>'
               '<cfRule type="cellIs" priority="2" operator="equal"><formula>0</formula></cfRule>'
               '</conditionalFormatting></worksheet>').encode()
        root = ET.fromstring(raw)
        cells = ET.tostring(root.find(M + 'sheetData'))
        remove_column_icons(root, 3)
        output = serialize(root, raw)
        self.assertEqual(cells, ET.tostring(root.find(M + 'sheetData')))
        self.assertTrue({'xr', 'x14ac'}.issubset(namespaces(output)))
        rules = root.findall(M + 'conditionalFormatting')
        self.assertEqual(rules[0].get('sqref'), 'C4:E4 A8')
        self.assertEqual(rules[0].find(M + 'cfRule').get('type'), 'cellIs')
        self.assertEqual(rules[1].get('sqref'), 'D4:E4 A8')
        self.assertEqual(rules[1].find(M + 'cfRule').get('type'), 'iconSet')

    def test_extended_icons_only_lose_quote_column(self):
        raw = (f'<worksheet xmlns="{NS}" '
               'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" '
               'xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main">'
               '<sheetData/><extLst><ext uri="test"><x14:conditionalFormattings>'
               '<x14:conditionalFormatting><x14:cfRule type="iconSet" id="history"/>'
               '<xm:sqref>B10:D12</xm:sqref></x14:conditionalFormatting>'
               '</x14:conditionalFormattings></ext></extLst></worksheet>').encode()
        root = ET.fromstring(raw)
        remove_column_icons(root, 3)
        xm = '{http://schemas.microsoft.com/office/excel/2006/main}'
        self.assertEqual(root.find('.//' + xm + 'sqref').text, 'B10:B12 D10:D12')
        ET.fromstring(serialize(root, raw))

    def test_quote_only_rule_removed_and_other_columns_untouched(self):
        root = ET.fromstring(f'<worksheet xmlns="{NS}"><sheetData/>'
                            '<conditionalFormatting sqref="$C$3:$C$5">'
                            '<cfRule type="iconSet"/></conditionalFormatting>'
                            '<conditionalFormatting sqref="D3:E5">'
                            '<cfRule type="iconSet"/></conditionalFormatting></worksheet>')
        remove_column_icons(root, 3)
        self.assertEqual([cf.get('sqref') for cf in root.findall(M + 'conditionalFormatting')], ['D3:E5'])

    def test_empty_extended_icon_container_is_removed(self):
        root = ET.fromstring(f'<worksheet xmlns="{NS}" xmlns:x14="urn:x14" xmlns:xm="urn:xm">'
                            '<sheetData/><extLst><ext uri="icons"><x14:conditionalFormattings>'
                            '<x14:conditionalFormatting><x14:cfRule type="iconSet"/>'
                            '<xm:sqref>C3</xm:sqref></x14:conditionalFormatting>'
                            '</x14:conditionalFormattings></ext></extLst></worksheet>')
        remove_column_icons(root, 3)
        self.assertIsNone(root.find(M + 'extLst'))


if __name__ == '__main__':
    unittest.main()

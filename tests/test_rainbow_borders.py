from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Border, Side

from market_rates.rainbow_borders import fix_sgd_bank_borders


class RainbowBorderTests(TestCase):
    def test_separates_merged_and_single_bank_blocks_without_changing_values(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rainbow.xlsx'
            book = Workbook(); sheet = book.active; sheet.title = 'SGD Promotional Rate'
            sheet['G3'] = 'BOC'; sheet['H3'] = 0.021; sheet['H4'] = 0.02
            sheet.merge_cells('G3:G4')
            sheet['G5'] = 'Maybank'; sheet['H5'] = 0.019
            sheet['G3'].border = Border(left=Side(style='double'))
            book.save(path)
            self.assertEqual(fix_sgd_bank_borders(path), 2)
            saved = load_workbook(path); sheet = saved['SGD Promotional Rate']
            self.assertEqual(sheet['G3'].value, 'BOC')
            self.assertEqual(sheet['H4'].value, 0.02)
            self.assertIn('G3:G4', [str(x) for x in sheet.merged_cells.ranges])
            for address in ('G3', 'G5'):
                self.assertEqual(sheet[address].border.top.style, 'thin')
                self.assertEqual(sheet[address].border.bottom.style, 'thin')
            self.assertEqual(sheet['G3'].border.left.style, 'double')

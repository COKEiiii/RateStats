"""Native plan writes retain both rates and nonnumeric disclosures."""
import sys
import ast
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from tempfile import TemporaryDirectory


@unittest.skipUnless(sys.platform == 'darwin', 'macOS Excel backend')
class NativePlanTests(unittest.TestCase):
    def test_smoke_opens_existing_seed_and_saves_without_save_as(self):
        from scripts.excel_native import action_excel_smoke
        from openpyxl import load_workbook
        with TemporaryDirectory() as folder:
            target=Path(folder)/'中文 空格'/'preview.pdf'
            app=Mock();book=Mock();sheet=Mock();book.sheets=[sheet]
            areas={address:Mock(value=5) for address in ('A1','A2','A3','B1:B2')}
            sheet.range.side_effect=areas.__getitem__
            def opened(owner,path):
                self.assertIs(owner,app)
                seed=load_workbook(path);seed.close()
                return book
            def exported(sh,area,path):
                self.assertIs(sh,sheet);self.assertEqual(area,'A1:C3')
                path.write_bytes(b'%PDF')
            with patch('scripts.excel_native.excel_app') as context, \
                 patch('scripts.excel_native.open_book',side_effect=opened), \
                 patch('scripts.excel_native.export_preview',side_effect=exported):
                context.return_value.__enter__.return_value=app
                action_excel_smoke(target)
            app.books.add.assert_not_called()
            book.save.assert_called_once_with()
            book.close.assert_called_once_with()
            self.assertEqual(areas['A3'].formula,[['=SUM(A1:A2)']])
            self.assertEqual(areas['A1'].value,2)
            areas['B1:B2'].insert.assert_called_once_with(shift='down')
            self.assertEqual(list(target.parent.glob('*.xlsx')),[])

    def test_smoke_cleanup_does_not_mask_the_original_export_failure(self):
        from scripts.excel_native import action_excel_smoke
        with TemporaryDirectory() as folder:
            app=Mock();book=Mock();book.sheets=[Mock()]
            areas={address:Mock(value=5) for address in ('A1','A2','A3','B1:B2')}
            book.sheets[0].range.side_effect=areas.__getitem__
            book.close.side_effect=RuntimeError('cleanup failed')
            with patch('scripts.excel_native.excel_app') as context, \
                 patch('scripts.excel_native.open_book',return_value=book), \
                 patch('scripts.excel_native.export_preview',side_effect=RuntimeError('PDF failed')):
                context.return_value.__enter__.return_value=app
                with self.assertRaisesRegex(RuntimeError,'^PDF failed$'):
                    action_excel_smoke(Path(folder)/'preview.pdf')
                context.return_value.__exit__.assert_called_once()
            self.assertEqual(list(Path(folder).glob('*.xlsx')),[])

    def test_smoke_failed_open_cleans_seed_and_private_context(self):
        from scripts.excel_native import action_excel_smoke
        with TemporaryDirectory() as folder:
            with patch('scripts.excel_native.excel_app') as context, \
                 patch('scripts.excel_native.open_book',side_effect=RuntimeError('open failed')):
                with self.assertRaisesRegex(RuntimeError,'open failed'):
                    action_excel_smoke(Path(folder)/'preview.pdf')
                context.return_value.__exit__.assert_called_once()
            self.assertEqual(list(Path(folder).glob('*.xlsx')),[])

    def test_sparse_comparison_write_preserves_intermediate_section_header(self):
        from scripts.excel_native import write_sparse_formulas
        area=SimpleNamespace(formula=[['=OLD()'],['当日增幅'],[None]],number_format='General')
        sheet=Mock();sheet.range.return_value=area
        write_sparse_formulas(sheet,5,{4:'=1-1',6:'=2-1'})
        self.assertEqual(area.formula,[['=1-1'],['当日增幅'],['=2-1']])

    def test_mixed_formula_matrices_keep_zero_and_literal_missing_marker(self):
        from scripts.excel_native import plan_array_value
        self.assertEqual(plan_array_value('-'),"'-")
        self.assertEqual(plan_array_value('=MAX(I4:I5)'),'=MAX(I4:I5)')
        self.assertEqual(plan_array_value(0),0)
        self.assertIsNone(plan_array_value(None))

    def test_batched_native_readback_keeps_sparse_assertions_strict(self):
        from scripts.excel_native import check_pending_cells
        area=Mock();area.options.return_value=SimpleNamespace(value=[[0,'-'],[None,.0165]])
        sheet=Mock();name=PropertyMock(return_value='quotes')
        type(sheet).name=name;sheet.range.return_value=area
        check_pending_cells([(sheet,3,4,0),(sheet,3,5,'-'),(sheet,4,5,.0165)])
        sheet.range.assert_called_once_with((3,4),(4,5))
        self.assertEqual(name.call_count,1)
        with self.assertRaisesRegex(AssertionError,'quotes 4/5'):
            check_pending_cells([(sheet,3,4,0),(sheet,4,5,.0175)])
        self.assertEqual(name.call_count,2)

    def test_interrupted_board_copies_are_preserved_before_replay(self):
        from scripts.run_board_wide import preserve_interrupted_excel_copies
        with TemporaryDirectory() as folder:
            out=Path(folder)
            report=out/'报表.xlsx'
            rainbow=out/'彩虹表.xlsx'
            report.write_bytes(b'partially written')
            rainbow.write_bytes(b'untouched template copy')
            moved=preserve_interrupted_excel_copies(out,{'report_output':str(report),'rainbow_output':str(rainbow)})
            self.assertEqual(len(moved),2)
            self.assertFalse(report.exists())
            self.assertFalse(rainbow.exists())
            self.assertEqual(Path(moved[0]).read_bytes(),b'partially written')
            self.assertEqual(Path(moved[1]).read_bytes(),b'untouched template copy')

    def test_cleanup_format_spans_cover_exact_updated_cells(self):
        from scripts.excel_native import contiguous_update_spans
        entries=[{'final_address': address} for address in ('E7','G7','F7','AA7','AB7','D4','D4')]
        self.assertEqual(list(contiguous_update_spans(entries)),[(4,4,4),(7,5,7),(7,27,28)])

    def test_saved_history_treats_only_whitespace_placeholder_as_blank(self):
        from scripts.check_board_wide import historical_value_equal
        self.assertTrue(historical_value_equal(' ', None))
        self.assertFalse(historical_value_equal('0', None))
        self.assertFalse(historical_value_equal(' ', 0))

    def test_inserted_history_column_skips_horizontal_merges(self):
        from openpyxl import Workbook
        from scripts.excel_native import inserted_column_copy_segments
        with TemporaryDirectory() as folder:
            template=Path(folder)/'template.xlsx'
            book=Workbook()
            sheet=book.active
            sheet.title='CNY挂牌'
            sheet.merge_cells('A1:G1')
            sheet.merge_cells('B4:F4')
            book.save(template)
            book.close()
            self.assertEqual(
                list(inserted_column_copy_segments(template,'CNY挂牌',3,6)),
                [(2,3),(5,6)],
            )

    def test_existing_merged_bank_group_does_not_need_a_plan_merge_entry(self):
        from scripts.excel_native import rebuild_deltas
        def sheet(matrix,merges=None):
            cells={};item=SimpleNamespace(used_range=SimpleNamespace(row=1,column=1,
                rows=SimpleNamespace(count=len(matrix)),columns=SimpleNamespace(count=len(matrix[0]))))
            def area(first,last=None):
                if last is not None:
                    if first[1]==last[1] and first[0]>1:
                        class FormulaArea:
                            api=Mock()
                            def clear_contents(self):
                                for r in range(first[0],last[0]+1):area((r,first[1])).formula=None
                            @property
                            def formula(self):return [[getattr(area((r,first[1])),'formula',None)] for r in range(first[0],last[0]+1)]
                            @formula.setter
                            def formula(self,values):
                                for r,value in zip(range(first[0],last[0]+1),values):area((r,first[1])).formula=[value]
                            @property
                            def number_format(self):return getattr(area(first),'number_format','General')
                            @number_format.setter
                            def number_format(self,value):
                                for r in range(first[0],last[0]+1):area((r,first[1])).number_format=value
                        return FormulaArea()
                    return SimpleNamespace(value=matrix)
                if isinstance(first,str):
                    from market_rates.report_xml import pos
                    first=pos(first)
                if first not in cells:
                    span=(merges or {}).get(first,(first[0],first[0]))
                    cells[first]=SimpleNamespace(merge_area=SimpleNamespace(row=span[0],last_cell=SimpleNamespace(row=span[1])))
                return cells[first]
            item.range=area
            return item,cells
        promo,cells=sheet([['银行','条件','利率','上期','当日增幅'],
                          ['CITI','产品 / personal',None,None,None],
                          [None,'产品 / personal',0.04,0.04,None],
                          ['Tenor: 2 months',None,None,None,None],
                          ['银行','全部公开报价',None,None,None],
                          ['CITI','产品 / personal',0.039,0.038,None]],{(2,1):(2,3)})
        board,_=sheet([[None,None,None,'当日增幅',None]])
        sgd,_=sheet([['当日最高报价变动']])
        book=SimpleNamespace(sheets={'USD促销':promo,'USD挂牌':board,'CNY挂牌':board,'SGD促销':sgd})
        plan={'histories':[{'sheet':'USD促销','bank_col':1,'current_col':3,'max_row':6,'bank_merges':[],
                           'inserts':[dict(kind='tenor',target_row=4,count=3)]}]}
        cells[(3,5)]=SimpleNamespace(formula='=$G3-$H3')
        with patch('scripts.excel_native.merge') as merge_delta:
            rebuild_deltas(book,plan)
        merge_delta.assert_called_once_with(promo,2,5,3,5)
        self.assertEqual(cells[(3,5)].formula,[[None]])
        formula=cells[(2,5)].formula[0][0]
        self.assertIn('INDEX(3:3,1,3)',formula)
        self.assertIn('INDEX(3:3,1,4)',formula)
        self.assertEqual(cells[(2,5)].number_format,'0.00%')
        self.assertEqual(cells[(5,5)].formula,[['当日增幅']])
        self.assertIn('INDEX(6:6,1,3)',cells[(6,5)].formula[0][0])

    def test_raw_appscript_properties_are_not_assigned_as_python_attributes(self):
        path=Path(__file__).resolve().parents[1]/'scripts/excel_native.py'
        source=path.read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.Assign):
                for target in node.targets:
                    self.assertNotIn('.api.',ast.get_source_segment(source,target))

    def test_empty_native_condition_collection_is_safe(self):
        from scripts.excel_native import clear_conditional_formats,appscript_kw
        area=Mock()
        area.api.format_conditions.get.return_value=appscript_kw.missing_value
        clear_conditional_formats(area)
        area.api.format_conditions.delete.assert_not_called()

    def test_address_plan_preserves_zero_missing_and_text(self):
        from scripts.excel_native import assign_cells
        entries = [
            {'address': 'C29', 'formula': '=MAX(I4:I5)', 'expected': 0.04},
            {'address': 'D29', 'formula': 0, 'expected': 0},
            {'address': 'E29', 'formula': '-', 'expected': '-'},
            {'address': 'H29', 'formula': '各产品门槛见USD促销', 'expected': '各产品门槛见USD促销'},
            {'address': 'I29', 'formula': '', 'expected': ''},
        ]
        cells = {item['address']: SimpleNamespace(number_format='General') for item in entries}
        sheet = Mock()
        sheet.range.side_effect = cells.__getitem__
        assign_cells(sheet, entries, number_format='0.00%')
        self.assertEqual(cells['C29'].formula, [['=MAX(I4:I5)']])
        self.assertEqual(cells['D29'].value, 0)
        self.assertEqual(cells['E29'].value, '-')
        self.assertEqual(cells['H29'].value, '各产品门槛见USD促销')
        self.assertEqual(cells['I29'].value, '')
        for address in ('C29', 'D29'):
            self.assertEqual(cells[address].number_format, '0.00%')
        for address in ('E29', 'H29', 'I29'):
            self.assertEqual(cells[address].number_format, 'General')

    def test_long_scalar_formula_uses_matrix_write(self):
        from scripts.excel_native import set_formula
        cell=SimpleNamespace()
        formula='='+ '+'.join('1' for _ in range(300))
        set_formula(cell,formula)
        self.assertEqual(cell.formula,[[formula]])

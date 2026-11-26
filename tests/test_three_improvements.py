"""Mutable source links and formula references must survive real user edits."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from market_rates.link_registry import bank_link,named_url,entries,official_url,discovery_folder
from market_rates.common import save
from market_rates.report_xml import Book,M
from market_rates.premier_details import validate_links,DETAIL_SHEET,record_key
from market_rates.premier_sync import rebuild_premier
from market_rates.report_format import format_report,freeze_values
from tests.test_premier_sync import selected,offer
from tests.test_report_format import fixture


class LinkEditTests(TestCase):
    def test_real_registry_has_unique_official_sources(self):
        records=entries('inputs/bank-links.xlsx')
        keys=[r['source_id'] for r in records]
        self.assertEqual(len(keys),len(set(keys)))
        for r in records:
            if r['url']:official_url(r['url'],r['bank'])
        self.assertTrue(bank_link('inputs/bank-links.xlsx','CIMB','SGD','board')['url'])
        dbs=next(r for r in records if r['source_id']=='DBS-others-promo')
        if dbs['url']:
            self.assertTrue(bank_link('inputs/bank-links.xlsx','DBS','others','promo')['url'])
        else:
            self.assertEqual(dbs['status'],'missing_link')
            with self.assertRaisesRegex(ValueError,'DBS others promo 网址为空'):
                bank_link('inputs/bank-links.xlsx','DBS','others','promo')

    def test_scb_changed_url_and_moved_row_reach_capture_configuration(self):
        from scripts.run_scb_pilot import configuration
        cells={'A181':'SCB','B181':'SGD','C181':'促销','D181':'https://www.sc.com/sg/new-location?offer=2'}
        with patch('market_rates.link_registry.read_xlsx',return_value={'Moved':cells}):
            # Configuration records the input checksum, so supply a temporary file.
            with TemporaryDirectory() as tmp:
                path=Path(tmp)/'links.xlsx';path.write_bytes(b'test')
                with patch('market_rates.link_registry.read_hyperlinks',return_value={}):
                    result=configuration(path)
        self.assertEqual(result['sources'][0]['urls'],[cells['D181']])
        self.assertEqual(result['pilot']['cell'],'D181')

    def test_supplement_key_is_independent_of_row_and_rejects_duplicates(self):
        cells={'A90':'OCBC','C90':'OCBC-terms','D90':'https://www.ocbc.com/new-terms'}
        with patch('market_rates.link_registry.read_xlsx',return_value={'补充来源':cells}):
            self.assertEqual(named_url('OCBC-terms','test.xlsx'),cells['D90'])
            cells.update(A91='OCBC',C91='OCBC-terms',D91=cells['D90'])
            with self.assertRaisesRegex(ValueError,'重复'):named_url('OCBC-terms','test.xlsx')

    def test_new_visible_url_takes_priority_over_old_hyperlink(self):
        cells={'A2':'SCB','B2':'SGD','C2':'促销','D2':'https://www.sc.com/new'}
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'links.xlsx';path.touch()
            with patch('market_rates.link_registry.read_xlsx',return_value={'Links':cells}),patch('market_rates.link_registry.read_hyperlinks',return_value={'Links':{'D2':'https://www.sc.com/old'}}):
                self.assertEqual(bank_link(path,'SCB','SGD','promo')['url'],cells['D2'])
                cells['D2']=''
                with self.assertRaises(ValueError):bank_link(path,'SCB','SGD','promo')

    def test_grouped_discovery_and_old_folder_names_resolve_by_identity(self):
        with TemporaryDirectory() as tmp:
            save(Path(tmp)/'status.json',[{'id':'old-D999','registry':[{'bank':'HSBC','currency_hint':c,'rate_type':'board'} for c in ['SGD','others']]}])
            self.assertEqual(discovery_folder(tmp,'HSBC-others-board'),Path(tmp)/'old-D999')
            with self.assertRaises(ValueError):discovery_folder(tmp,'HSBC-SGD-promo')


class PremierReferenceTests(TestCase):
    def make_book(self,path):
        fixture(path,date(2026,10,7))
        book=Book(path);book.add_sheet('Premier Rates')
        rows=selected(offer(),offer(audience='premier_elite',rate_pct='3.7'))
        rebuild_premier(book,rows,{'SGD':'2026-10-08','USD':'2026-10-08'},{})
        return book,rows

    def test_source_sorting_does_not_change_reference_identity(self):
        with TemporaryDirectory() as tmp:
            book,rows=self.make_book(Path(tmp)/'input.xlsx')
            checks=validate_links(book);detail=book.sheets[DETAIL_SHEET]
            before={r:[detail.value(r,c) for c in range(1,14)] for r in [4,5]}
            for target,original in [(4,5),(5,4)]:
                for c,v in enumerate(before[original],1):detail.put(target,c,v)
            self.assertEqual(validate_links(book),checks)
            self.assertNotEqual(record_key(rows[0]),record_key(rows[1]))

    def test_excel_saved_unquoted_sheet_name_keeps_strict_local_links(self):
        with TemporaryDirectory() as tmp:
            book,_=self.make_book(Path(tmp)/'input.xlsx')
            for cell in book.sheets['Premier Rates'].cells.values():
                formula=cell.find(M+'f')
                if formula is not None:formula.text=formula.text.replace("'"+DETAIL_SHEET+"'!",DETAIL_SHEET+'!')
            self.assertEqual(len(validate_links(book)),2)

    def test_formatter_retains_formula_and_current_detail(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);book,_=self.make_book(root/'input.xlsx');book.save(root/'input.xlsx')
            fixture(root/'template.xlsx',date(2026,10,7),sources=False)
            template=Book(root/'template.xlsx');template.add_sheet('Premier Rates')
            rebuild_premier(template,selected(offer()),{'USD':'2026-10-08'},{})
            template.drop_sheet(DETAIL_SHEET);template.save(root/'template.xlsx')
            result=format_report(root/'input.xlsx',root/'template.xlsx',root/'formatted.xlsx')
            saved=Book(root/'formatted.xlsx')
            self.assertEqual(len(validate_links(saved)),2)
            self.assertIn(DETAIL_SHEET,saved.sheets)
            self.assertEqual(result['verification']['premier_formula_count'],2)

    def test_missing_formula_conflicting_record_and_stale_rows_fail(self):
        with TemporaryDirectory() as tmp:
            book,rows=self.make_book(Path(tmp)/'input.xlsx')
            cell=next(c for c in book.sheets['Premier Rates'].cells.values() if c.find(M+'f') is not None)
            cell.remove(cell.find(M+'f'))
            with self.assertRaisesRegex(ValueError,'固定数字'):validate_links(book)
            rebuild_premier(book,rows[:1],{'USD':'2026-10-08'},{})
            self.assertEqual(len(validate_links(book)),1)
            self.assertEqual(book.sheets[DETAIL_SHEET].maxrow,4)
            detail=book.sheets[DETAIL_SHEET]
            detail.put(5,1,detail.value(4,1))
            with self.assertRaisesRegex(ValueError,'重复'):validate_links(book)

"""Source-independent completeness and retail choice regression cases."""
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from market_rates.quote_selection import personal_offer
from market_rates.source_inventory import boc_inventory,verify_source_inventory
from market_rates.sgd_source_alignment import align_tables
from market_rates.quote_layout import extend_sgd
from market_rates.rules import build_views,display_tenor
from market_rates.demo import offer
from market_rates.fx_promo import validate_source_absence,dbs_promo_links


class SourceCoverageTests(TestCase):
    def page(self, rates, pid='boc-sgd-mobile'):
        text='货币\t期限\t手机银行\n起存金额\t年利率(%)\n新元\t3个月\t500\t'+rates[0]
        for minimum,rate in zip(['20,000','200,000'],rates[1:]):text+='\n'+minimum+'\t'+rate
        return dict(id='latest',bank='BOC',units=[dict(kind='rates',product_ids=[pid],text=text)])

    def rows(self, rates):
        p=self.page(rates)
        rows=[offer('BOC','boc-sgd-mobile',rate='1.6')]
        rows[0].update(evidence=[dict(page_id='latest',locator='row',quote='1.6')])
        align_tables(rows,[],[p],'llm')
        return p,rows

    def test_latest_notice_alone_does_not_inherit_old_higher_tier(self):
        _,rows=self.rows(['1.60'])
        self.assertEqual(personal_offer(rows)['rate_pct'],'1.60')
        self.assertEqual(len(rows),1)

    def test_all_latest_amount_columns_participate_in_max(self):
        for rates,expected,minimum in [(['1.60','1.65'],'1.65','20000'),(['1.60','1.65','1.75'],'1.75','200000')]:
            with self.subTest(rates=rates):
                p,rows=self.rows(rates);audit=verify_source_inventory(rows,[p]);win=personal_offer(rows)
                self.assertEqual((win['rate_pct'],win['amount_min']),(expected,minimum))
                self.assertEqual(audit[0]['expected'],len(rates))
                self.assertTrue(all(r['tenor_value']==3 for r in rows))

    def test_missing_duplicate_and_misaligned_rows_fail_closed(self):
        p,rows=self.rows(['1.60','1.65','1.75'])
        bad=deepcopy(rows);bad[1]['tenor_value']=4
        for changed in (rows[:-1],rows+[deepcopy(rows[0])],bad):
            with self.assertRaisesRegex(ValueError,'coverage mismatch'):verify_source_inventory(changed,[p])

    def test_unrecognized_grid_cannot_claim_zero_expected(self):
        with self.assertRaises(ValueError):boc_inventory('新元\n起存金额\n年利率\n表格不可读取')

    def test_singfinance_two_amount_columns_recover_all_tiers(self):
        text='Tenure\tMinimum Deposit Amount\n$1,000\t$10,000\n3 months\t1.45% p.a.\t1.50% p.a.\n6 months\t1.50% p.a.\t1.80% p.a.'
        p=dict(id='sf',bank='SingFinance',units=[dict(kind='rates',product_ids=['singfinance-sgd-online'],text=text)])
        rows=[offer('SingFinance','singfinance-sgd-online',rate='1.8')]
        align_tables(rows,[],[p],'llm')
        self.assertEqual(len(rows),4)
        self.assertEqual(personal_offer([r for r in rows if r['tenor_value']==3])['amount_min'],'10000')
        self.assertEqual(verify_source_inventory(rows,[p])[0]['verified'],4)
        with self.assertRaises(ValueError):verify_source_inventory(rows[1:],[p])

    def test_literal_inventory_does_not_trust_model_row_count(self):
        p=dict(id='p',bank='OTHER',units=[dict(kind='rates',product_ids=['product'],text='Tenor\tRate\n3 months\t1.60%\n6 months\t1.70%')])
        rows=[offer('OTHER','product',rate='1.6')]
        with self.assertRaisesRegex(ValueError,'Source rate coverage mismatch'):verify_source_inventory(rows,[p])

    def test_boc_welcome_and_mobile_keep_their_own_period(self):
        terms='本次个人定期存款促销利率有效期为2026年10月19日至2026年10月25日。\n**2026年9月14日至2026年10月31日新客户专属。'
        pages=[];rows=[];bound_terms=[]
        for pid in ['boc-sgd-mobile','boc-sgd-welcome']:
            p=self.page(['1.60'],pid);p['id']=pid
            p['units'].append(dict(kind='terms',product_ids=[pid],text=terms))
            pages.append(p);rows.append(offer('BOC',pid,rate='1.6'))
            bound_terms.append(dict(page_id=pid,product_id=pid,locator=pid+':2',text=terms))
        align_tables(rows,bound_terms,pages,'llm')
        dates={r['product_id']:(r['valid_from'],r['valid_to']) for r in rows}
        self.assertEqual(dates['boc-sgd-mobile'],('2026-10-19','2026-10-25'))
        self.assertEqual(dates['boc-sgd-welcome'],('2026-09-14','2026-10-31'))
        verify_source_inventory(rows,pages)
        rows[-1]['valid_to']='2026-10-25'
        with self.assertRaisesRegex(ValueError,'period mismatch'):verify_source_inventory(rows,pages)

    def test_private_investment_bundle_cannot_be_personal(self):
        rows=[offer('CITI','citi-private-investment-bundle',audience='preferred',rate='2')]
        changes=align_tables(rows,[],[],'llm')
        self.assertEqual(rows[0]['audience'],'private')
        self.assertIsNone(personal_offer(rows))
        self.assertTrue(any(c['field']=='audience' for c in changes))

    def test_source_associations_reject_wrong_amount_and_channel(self):
        from market_rates.source_inventory import verify_retail_slots
        page=dict(bank='OCBC',units=[dict(kind='rates',product_ids=['ocbc-sgd-promo'],text='Singapore Dollar\t3\tBranch\t1.60%\tS$20,000\n6\tOnline\t1.80%\tS$50,000')])
        rows=[dict(offer('OCBC','ocbc-sgd-promo',rate='1.60'),tenor_value=3,channel='branch',amount_min='20000'),dict(offer('OCBC','ocbc-sgd-promo',rate='1.80'),tenor_value=6,channel='online',amount_min='50000')]
        verify_retail_slots(rows,[page])
        for field,value in [('amount_min','500'),('channel','online'),('tenor_value',2)]:
            changed=deepcopy(rows);changed[0][field]=value
            with self.assertRaisesRegex(ValueError,'association mismatch'):verify_retail_slots(changed,[page])


class RetailChoiceTests(TestCase):
    def r(self,rate,minimum='10000',audience='personal',**extra):
        return dict(rate_pct=rate,amount_min=minimum,audience=audience,amount_currency='SGD',**extra)

    def test_all_and_fresh_funds_are_eligible_exclusive_segments_are_not(self):
        rs=[self.r('2',audience='premier'),self.r('1.9',audience='preferred'),self.r('1.8',audience='private'),self.r('1.7',audience='corporate'),self.r('1.65',audience='all',fresh_funds='yes')]
        self.assertIs(personal_offer(rs),rs[-1])
        self.assertIsNone(personal_offer(rs[:-1]))

    def test_missing_legacy_currency_layout_is_not_personal_ineligibility(self):
        from market_rates.quote_selection import comparison_eligible
        row=self.r('1.65',hold_reason='现有模板无CHF区域，仅留明细')
        self.assertTrue(comparison_eligible(row))
        self.assertIs(personal_offer([row]),row)
        self.assertFalse(comparison_eligible(dict(row,hold_reason='促销未生效或已截止')))

    def test_equal_maxima_choose_lower_minimum_with_own_terms(self):
        rs=[self.r('1.75','200000',channel='branch',conditions='A',evidence=['A']),self.r('1.75','20000',channel='online',conditions='B',evidence=['B'])]
        self.assertIs(personal_offer(rs),rs[1])
        self.assertEqual(rs[1]['evidence'],['B'])
        rs[1]['amount_currency']='USD'
        self.assertIs(personal_offer(rs),rs[0])

    def test_ties_preserve_stable_order_and_unknown_is_not_zero(self):
        rs=[self.r('0',None),self.r('0','1000'),self.r('0','1000')]
        self.assertIs(personal_offer(rs),rs[1])

    def test_reordered_views_use_the_same_verified_source_row(self):
        rs=[self.r('1.65',input_row=9,conditions='later'),self.r('1.65',input_row=4,conditions='first')]
        self.assertIs(personal_offer(rs),rs[1])
        self.assertIs(personal_offer(list(reversed(rs))),rs[1])

    def test_all_public_tiers_and_products_survive_with_no_personal_fallback(self):
        rs=[offer('B','one',rate='1.6'),offer('B','one',rate='1.65'),offer('B','two',audience='premier',rate='2')]
        v=build_views(rs,{}, {},'2026-09-25')
        self.assertEqual(sum(len(g['details']) for g in v['groups']),3)
        self.assertEqual(v['highest']['B/6M']['rate_pct'],'1.65')
        self.assertIsNone(next(g for g in v['groups'] if g['product_id']=='two')['main_pct'])

    def test_actual_tenor_overrides_old_aliases_and_layout_extends(self):
        r=offer('BOC','boc-sgd-welcome');r['tenor_value']=8
        self.assertEqual(display_tenor(r,{'tenor_map':{'BOC/boc-sgd-welcome/8M':'9M'}}),'8M')
        snap=dict(report=[dict(row=2,values=['1 Months',None,None])],rainbow={'1':dict(title='1M')})
        sections=extend_sgd(snap,[dict(bank='BOC',display_tenor=t) for t in ['2M','4M','8M','10M']],{})
        self.assertEqual([s['tenor'] for s in sections],['2M','4M','8M','10M'])


class DbsAbsenceTests(TestCase):
    def section(self):
        return dict(bank='DBS',currency='USD',status='no_current_promotion',rows=[],task_ids=['heading'],page_id='public',confirmation_date='2026-10-22',resolved_url='https://www.dbs.com.sg/personal/accounts/fixed-deposit',public_text='Fixed Deposit Accounts\nUSD board rate',public_links=[])

    def test_confirmed_absence_is_normal_and_specific_to_dbs(self):
        s=self.section();self.assertEqual(validate_source_absence(s)['status'],'no_current_promotion')
        s['bank']='HSBC'
        with self.assertRaises(ValueError):validate_source_absence(s)

    def test_new_campaign_link_prevents_reusing_absence(self):
        s=self.section();s['public_links']=[dict(url='https://www.dbs.com.sg/personal/promotion/new-usd-fd',text='USD Fixed Deposit Promotion')]
        self.assertTrue(dbs_promo_links(s['public_links']))
        with self.assertRaises(ValueError):validate_source_absence(s)


class LayoutAndCatalogTests(TestCase):
    def test_saved_evidence_replays_are_not_daily_production_seeds(self):
        from market_rates.common import offline_replay
        self.assertFalse(offline_replay({'metadata':[dict(provider='ollama')]}))
        self.assertTrue(offline_replay({'offline_replay':{'source':'archive'}}))
        self.assertTrue(offline_replay({'metadata':[dict(source_sha256='abc',method='Rebind saved model rows to archived literal source grids; no new capture or manual approval')]}))

    def test_board_extension_adds_actual_tenor_without_old_values(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import openpyxl
        from market_rates.quote_layout import prepare_board_layout
        from market_rates.report_xml import Book
        with TemporaryDirectory() as d:
            before=Path(d)/'before.xlsx';after=Path(d)/'after.xlsx'
            wb=openpyxl.Workbook();ws=wb.active;ws.title='USD挂牌'
            ws['B2']='1M';ws['D3']='2026-10-21';ws['B4']='BOC';ws['E4']=0.03;wb.save(before)
            history=dict(sheet='USD挂牌',bank_col=2,current_col=4,old_date_rows=[3],extensions=[dict(start=10,tenor='2M',rows=[dict(row=12,bank='BOC',amount='USD 500'),dict(row=13,bank='',amount='USD 20000')],banks=[dict(start=12,end=13,bank='BOC')])])
            prepare_board_layout(before,[history],after);s=Book(after).sheets['USD挂牌']
            self.assertEqual(s.value(10,2),'2M');self.assertEqual(s.value(12,2),'BOC')
            self.assertIsNone(s.value(12,5));self.assertEqual(s.value(4,5),Decimal('.03'))

    def test_empty_dynamic_section_keeps_original_history_and_does_not_copy_it(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import date
        from tests.test_report_format import fixture
        from market_rates.report_xml import Book
        from market_rates.quote_layout import prepare_sgd_layout
        from market_rates.table_validation import sha
        with TemporaryDirectory() as d:
            before=Path(d)/'before.xlsx';after=Path(d)/'after.xlsx';fixture(before,date(2026,10,22));original=sha(before)
            prepare_sgd_layout(before,[dict(start=20,tenor='8M',banks=['HLF'])],after)
            old,new=Book(before),Book(after);sheet=new.sheets['SGD促销']
            self.assertEqual(sha(before),original)
            self.assertEqual(sheet.value(20,1),'8 Months')
            self.assertEqual(sheet.value(23,1),'HLF')
            self.assertIsNone(sheet.value(23,4))
            self.assertEqual(sheet.value(5,4),old.sheets['SGD促销'].value(5,4))
            self.assertEqual(new.parts['xl/styles.xml'],old.parts['xl/styles.xml'])

    def test_equal_price_products_and_ties_remain_distinct_in_saved_catalog(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import date
        from tests.test_report_format import fixture
        from market_rates.public_quotes import write_catalog,PUBLIC,WINNERS
        from market_rates.report_xml import Book
        with TemporaryDirectory() as d:
            path=Path(d)/'quotes.xlsx';fixture(path,date(2026,10,22));rows=[]
            for product in ['one','two']:
                r=offer('B',product,rate='1.65');r.update(product_name=product,quote_id=product,source_urls=['https://example.test/'+product],display_status='verified');rows.append(r)
            cat=dict(records=rows,winners=[dict(bank='B',currency='SGD',rate_type='promo',tenor='6M',quote_id='one',rate_pct='1.65',tied_quotes=['one','two'])])
            result=write_catalog(path,cat);book=Book(path)
            self.assertTrue(result['passed']);self.assertEqual(result['quotes'],2)
            self.assertEqual([book.sheets[PUBLIC].value(r,6) for r in [2,3]],['one','two'])
            self.assertEqual(book.sheets[WINNERS].value(2,12),'one\ntwo')
            write_catalog(path,dict(cat,records=rows[:1]))
            repeated=Book(path)
            self.assertEqual(repeated.sheets[PUBLIC].maxrow,2)
            self.assertEqual(list(repeated.sheets).count(PUBLIC),1)

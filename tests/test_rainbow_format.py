"""Selection/qualification regression checks; no Excel installation required."""
from datetime import date
from dataclasses import asdict
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

from market_rates.rainbow_format import (
    Quote, Reader, Painter, amount_text, brief_notes, rate_value, select_quotes, select_sgd_promotions, source_sections,
    validate_sgd_selection, BUSINESS,
)
from market_rates.report_xml import M


def audit():
    return {k:[] for k in ('excluded_audience','ties','candidates','issues','missing_removed')}


def quote(rate, minimum=10000, audience='Personal', **kw):
    return Quote(bank=kw.pop('bank','BANK'),tenor='6M',currency='SGD',category='促销',
                 rate=rate_value(D(rate)),minimum=D(minimum),audience=audience,
                 amount_currency='SGD',**kw)


class SelectionTests(unittest.TestCase):
    def test_promotions_keep_audiences_and_rank_banks_by_personal_only(self):
        qs=[quote('.02',bank='A'),quote('.05',bank='A',audience='Premier'),
            quote('.025',bank='B'),quote('.026',bank='B',audience='Preferred')]
        result=select_sgd_promotions(qs)
        self.assertEqual([q.bank for q in result],['B','B','A','A'])
        self.assertEqual([q.audience for q in result],['Personal','Preferred','Personal','Premier'])
        self.assertCountEqual(result,qs)

    def test_promo_keeps_one_personal_maximum_with_its_own_condition(self):
        qs=[quote('.02',10000,channel='online'),quote('.019',1000,channel='counter'),
            quote('.02',50000,channel='counter'),quote('.025',10000,audience='Premier')]
        log=audit();result=select_sgd_promotions(qs,log)
        self.assertEqual(result,[qs[0],qs[1],qs[2],qs[3]])
        self.assertTrue(result[0].condition.startswith('S$≥10K\nPersonal online'))
        self.assertNotIn('counter',result[0].condition)
        self.assertEqual(log.get('excluded_personal_quotes',[]),[])

    def test_all_premier_and_preferred_tiers_survive_personal_filter(self):
        qs=[quote('.02'),quote('.03',audience='Premier'),quote('.025',50000,audience='Premier'),
            quote('.027',audience='Preferred'),quote('.019',audience='all')]
        result=select_sgd_promotions(qs)
        self.assertCountEqual(result,qs)
        self.assertEqual(sum(q.audience=='Personal' for q in result),1)

    def test_tied_personal_minimum_keeps_its_own_eligibility_and_inclusive_bound(self):
        qs=[quote('.02',lower='>',channel='counter',notes='仅续存'),
            quote('.02',lower='≥',channel='online',notes='须新资金')]
        result=select_sgd_promotions(qs)
        self.assertEqual(result,qs)
        self.assertEqual(result[0].notes,'仅续存')

    def test_incomparable_personal_amount_currencies_keep_source_order(self):
        qs=[quote('.02',10000,order=1),quote('.02',100,order=2)]
        qs[1].amount_currency='USD'
        self.assertEqual(select_sgd_promotions(qs),qs)

    def test_exclusive_only_bank_is_kept_after_zero_personal_and_all_is_personal(self):
        qs=[quote('.05',bank='A',audience='Premier'),quote('0',bank='B'),quote('.01',bank='C',audience='all')]
        self.assertEqual([q.bank for q in select_sgd_promotions(qs)],['C','B','A'])

    def test_tied_personal_banks_keep_source_order_despite_other_customer_rates(self):
        qs=[quote('.02',bank='Z'),quote('.021',bank='Z',audience='Premier'),
            quote('.02',bank='A'),quote('.06',bank='A',audience='Preferred')]
        self.assertEqual([q.bank for q in select_sgd_promotions(qs)],['Z','Z','A','A'])

    def test_product_addition_and_personal_withdrawal_update_promo_ranking(self):
        a=quote('.02',bank='A');b=quote('.025',bank='B');exclusive=quote('.06',bank='A',audience='Premier')
        new=quote('.03',bank='A')
        self.assertEqual([q.bank for q in select_sgd_promotions([a,b,exclusive])],['B','A','A'])
        self.assertEqual([q.bank for q in select_sgd_promotions([a,b,exclusive,new])],['A','A','A','B'])
        self.assertEqual(select_sgd_promotions([a,b,exclusive,new])[0],new)
        self.assertEqual([q.bank for q in select_sgd_promotions([b,exclusive])],['B','A'])

    def test_personal_does_not_inherit_premier_max(self):
        qs=[quote('.021',audience='Premier'),quote('.020',audience='Personal'),quote('.019',audience='all')]
        self.assertEqual(select_quotes(qs,'personal',audit())[0].rate,D('.020'))

    def test_exclusive_audience_without_personal_disappears(self):
        self.assertEqual(select_quotes([quote('.03',audience='Preferred')],'personal',audit()),[])

    def test_equal_rates_choose_lowest_amount_and_own_channel(self):
        q=select_quotes([quote('.02',100000,channel='counter'),quote('.02',10000,channel='online')],'max',audit())[0]
        self.assertEqual((q.minimum,q.channel),(D(10000),'online'))

    def test_inclusive_lower_bound_wins_same_minimum(self):
        q=select_quotes([quote('.02',lower='>'),quote('.02',lower='≥')],'max',audit())[0]
        self.assertEqual(q.lower,'≥')

    def test_board_keeps_distinct_rates_deduplicates_equal_rates(self):
        qs=[quote('.02',100000),quote('.02',10000),quote('.01',1000)]
        result=select_quotes(qs,'dedup_rate',audit())
        self.assertEqual([(q.rate,q.minimum) for q in result],[(D('.01'),D(1000)),(D('.02'),D(10000))])

    def test_bank_blocks_rank_by_maximum_and_do_not_interleave(self):
        qs=[quote('.03',50000,bank='A'),quote('.01',1000,bank='A'),
            quote('.025',10000,bank='B'),quote('.015',20000,bank='B')]
        result=select_quotes(qs,'dedup_rate',audit())
        self.assertEqual([q.bank for q in result],['A','A','B','B'])
        self.assertEqual([q.rate for q in result],[D('.01'),D('.03'),D('.025'),D('.015')])

    def test_new_product_and_deleted_winner_recompute_ranking(self):
        a=quote('.01',bank='A');b=quote('.02',bank='B');new=quote('.03',bank='A')
        self.assertEqual([q.bank for q in select_quotes([a,b],'dedup_rate',audit())],['B','A'])
        self.assertEqual([q.bank for q in select_quotes([a,b,new],'dedup_rate',audit())],['A','A','B'])
        self.assertEqual([q.bank for q in select_quotes([a,b],'dedup_rate',audit())],['B','A'])

    def test_float_tails_tie_but_display_rounding_does_not(self):
        self.assertEqual(rate_value(D('.020499999999999997')),rate_value(D('.020500000000000001')))
        self.assertNotEqual(rate_value(D('.0205001')),rate_value(D('.0205002')))

    def test_zero_is_valid_and_percent_text_becomes_numeric(self):
        self.assertEqual(select_quotes([quote('0')],'max',audit())[0].rate,D('0'))
        self.assertIsNone(rate_value('-'))
        self.assertEqual(rate_value('1.90%'),D('.019'))

    def test_amount_keeps_upper_bound_exclusivity_and_equivalent_currency(self):
        self.assertEqual(amount_text('USD等值',D(10000),D(200000),'≥','<'),'USD等值 ≥10K / <200K')

    def test_different_amount_currencies_are_not_numerically_compared(self):
        a=quote('.02',10000,order=1);b=quote('.02',100,order=2);b.amount_currency='EUR'
        result=select_quotes([a,b],'max',audit())[0]
        self.assertEqual(result.amount_currency,'SGD')


class PromoValidationTests(unittest.TestCase):
    def sample(self):
        quotes=[quote('.02',channel='online'),quote('.018',channel='counter'),
                quote('.05',audience='Premier'),quote('.04',audience='Preferred')]
        original=[asdict(q) for q in quotes]
        selected=[dict(asdict(q),sheet=BUSINESS[0]) for q in select_sgd_promotions(quotes)]
        return dict(candidates=original,selected=selected)

    def test_accepts_personal_maximum_with_exclusive_quotes(self):
        validate_sgd_selection(self.sample())

    def test_missing_exclusive_quote_is_rejected(self):
        data=self.sample();data['selected'].pop()
        with self.assertRaisesRegex(ValueError,'Public SGD quotes'):
            validate_sgd_selection(data)

    def test_second_personal_row_is_rejected(self):
        data=self.sample();data['selected'].append(dict(data['candidates'][1],sheet=BUSINESS[0]))
        with self.assertRaisesRegex(ValueError,'Public SGD quotes'):
            validate_sgd_selection(data)

    def test_condition_from_lower_rate_is_not_attached_to_winner(self):
        data=self.sample();data['selected'][0]['channel']='counter'
        with self.assertRaisesRegex(ValueError,'Public SGD quotes'):
            validate_sgd_selection(data)


class QualificationTests(unittest.TestCase):
    def note(self,raw,bank='BANK',actual='6M',tenor='6M',end=None):
        return brief_notes(raw,bank,actual,tenor,date(2026,10,7),end)

    def test_no_fresh_funds_requirement_is_not_reversed(self):
        note=self.note('个人合计< S$1,000,000；不限新资金')
        self.assertIn('不限新资金',note)
        self.assertNotIn('须新资金',note)
        self.assertIn('个人合计< S$1,000,000',note)

    def test_actual_tenor_and_expired_offer_retained(self):
        note=self.note('',actual='8M',tenor='9M',end='2026-10-04')
        self.assertIn('实际8M',note);self.assertIn('公告已截止',note)

    def test_renewal_only_retained(self):
        self.assertIn('仅同期限续存，不接受新存入',self.note('仅同期限旧存款续期，不接受新存入'))

    def test_wrong_bank_terms_do_not_become_eligibility(self):
        self.assertEqual(self.note('Standard Chartered '*5,bank='Maybank'),'源备注混入其他银行条款，资格待核')


class FakeSheet:
    def __init__(self,name,values):
        self.name=name;self.values=values
        self.maxrow=max(r for r,c in values)
        self.maxcol=max(c for r,c in values)
        self.cells={rc:ET.Element(M+'c') for rc in values}
    def value(self,r,c):return self.values.get((r,c))


class ReaderRegressionTests(unittest.TestCase):
    def test_partial_new_tenor_header_cannot_silently_drop_its_quotes(self):
        reader=Reader.__new__(Reader)
        view=FakeSheet(BUSINESS[0],{(2,31):'8M',(3,31):'BOC',(3,32):D('.0218')})
        with self.assertRaisesRegex(ValueError,'Incomplete tenor header triple'):reader.blocks(view,2,4,'SGD','促销')
        view.values[2,32]='Promo Rate';view.values[2,33]='Condition'
        reader.quote=lambda *args,**kwargs:quote('.0218',bank='BOC')
        self.assertEqual(len(reader.blocks(view,2,4,'SGD','促销')['8M']),1)

    def test_source_coverage_uses_input_rows_independently_of_parsed_views(self):
        from market_rates.rainbow_format import verify_source_quote_coverage
        sheets={name:FakeSheet(name,{(4,column):D('.02')}) for name,column in
                [('明细_20261007',8),('挂牌全批_20261007',9),('外币促销_20261007',9)]}
        reader=SimpleNamespace(as_of=date(2026,10,7),book=SimpleNamespace(sheets=sheets))
        selected=[dict(source=name,source_row=4,actual='8M',tenor='8M') for name in sheets]
        self.assertEqual([r['expected'] for r in verify_source_quote_coverage(reader,selected)],[1,1,1])
        for invalid in (selected[1:],selected+[selected[0]],selected+[dict(source='明细_20261007',source_row=5,actual='8M',tenor='8M')],
                        [dict(selected[0],tenor='9M')]+selected[1:]):
            with self.assertRaisesRegex(ValueError,'Visible public quotes'):verify_source_quote_coverage(reader,invalid)

    def test_promo_bank_merge_records_personal_ranking_and_retains_labels(self):
        import tempfile
        from pathlib import Path
        from tests.test_report_format import fixture
        from market_rates.report_xml import Book
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'a.xlsx',Path(folder)/'b.xlsx'
            fixture(a,date(2026,10,7));fixture(b,date(2026,10,7))
            target,template=Book(a),Book(b)
            for book in (target,template):
                s=book.sheets['SGD促销'];s.name='SGD Promotional Rate';book.sheets={s.name:s}
                for r in (3,5):
                    for c in (1,2,3):s.put(r,c,'style','0')
            sheet=target.sheets['SGD Promotional Rate'];p=Painter(target,template);p.reset(sheet,3,3)
            qs=select_sgd_promotions([quote('.02',bank='A'),quote('.05',bank='A',audience='Premier'),
                                      quote('.03',bank='B'),quote('.04',bank='B',audience='Preferred')])
            p.blocks(sheet,3,1,{'6M':qs},3,5,{'6M':1},condition=True,group_banks=True,personal_rank=True)
            self.assertEqual([sheet.value(r,1) for r in (4,5,6,7)],['B',None,'A',None])
            self.assertEqual([g['ranking_rate'] for g in p.bank_groups],[D('.03'),D('.02')])
            self.assertEqual([g['highest_rate'] for g in p.bank_groups],[D('.04'),D('.05')])
            self.assertIn('Preferred',sheet.value(5,3));self.assertIn('Premier',sheet.value(7,3))
            self.assertIn('A6:A7',[n.get('ref') for n in sheet.root.findall(M+'mergeCells/'+M+'mergeCell')])

    def test_constant_long_tenor_quote_is_reconciled_to_detail(self):
        detail=FakeSheet('外币促销_20261007',{(4,1):'BOC',(4,2):'CNY',(4,3):'CNY',
            (4,4):'手机银行普通促销',(4,5):'18M',(4,6):'personal / 手机银行',
            (4,7):D(50000),(4,9):D('.015'),(4,11):'CNY',(4,13):'2026-10-04'})
        view=FakeSheet('USD Rate + Other Currency Rates',{(25,22):D('.015')})
        reader=Reader.__new__(Reader);reader.book=SimpleNamespace(sheets={detail.name:detail})
        reader.audit=audit();reader.order=0;reader.as_of=date(2026,10,7)
        q=reader.quote(view,25,22,'BOC','CNY','促销','18M','RMB50K')
        self.assertEqual((q.rate,q.minimum,q.source_row),(D('.015'),D(50000),4))
        self.assertIn('公告已截止',q.notes)

    def test_missing_detail_fails_instead_of_deleting_quote(self):
        view=FakeSheet('view',{(1,2):D('.015')})
        reader=Reader.__new__(Reader);reader.book=SimpleNamespace(sheets={})
        reader.audit=audit();reader.order=0;reader.as_of=date(2026,10,7)
        with self.assertRaisesRegex(ValueError,'No matching verified detail'):
            reader.quote(view,1,2,'BOC','CNY','促销','18M')


class LayoutTests(unittest.TestCase):
    def test_bank_labels_merge_vertically_with_all_rates_intact(self):
        import tempfile
        from pathlib import Path
        from tests.test_report_format import fixture
        from market_rates.report_xml import Book
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'template.xlsx';fixture(path,date(2026,10,7))
            template=Book(path);target=Book(path)
            for book in (template,target):
                s=book.sheets['SGD促销'];s.name='SGD Board Rate'
                book.sheets={s.name:s}
                for r in (3,5):
                    for c in (1,2,3):s.put(r,c,'style','0')
            sheet=target.sheets['SGD Board Rate'];p=Painter(target,template);p.reset(sheet,3,3)
            qs=select_quotes([quote('.03',50000,bank='A'),quote('.01',1000,bank='A'),quote('.02',bank='B')],'dedup_rate',audit())
            p.blocks(sheet,3,1,{'6M':qs},3,5,{'6M':1},group_banks=True)
            self.assertEqual([sheet.value(r,1) for r in (4,5,6)],['A',None,'B'])
            self.assertIn('A4:A5',[n.get('ref') for n in sheet.root.findall(M+'mergeCells/'+M+'mergeCell')])
            self.assertEqual([sheet.value(r,2) for r in (4,5,6)],[D('.01'),D('.03'),D('.02')])


class SectionChangesTests(unittest.TestCase):
    def reader(self, new_title=None, tenor='6M'):
        from market_rates.rainbow_format import BUSINESS
        fx=FakeSheet(BUSINESS[2],{(3,1):'USD Board Rates',(10,1):'CNY Board Rates',
            (11,2):'1M',(11,3):'3M',(11,4):'6M',(11,5):'9M',(11,6):'12M',
            (11,7):'18M',(11,8):'24M',(20,1):'Other Currency Rates',(25,1):None})
        if new_title:fx.values[24,1]=new_title
        sheets={name:FakeSheet(name,{(1,1):None}) for name in BUSINESS[:2]};sheets[BUSINESS[2]]=fx
        return SimpleNamespace(book=SimpleNamespace(sheets=sheets),audit=audit(),
            blocks=lambda sheet,header,end,currency,category:{tenor:[]})

    def test_optional_promotion_sections_may_all_disappear(self):
        _,_,sections,matrices=source_sections(self.reader())
        self.assertEqual(sections['EUR','促销'],{})
        self.assertEqual(sections['USD','促销'],{})
        self.assertEqual(set(matrices),{'CNY','OTHER'})

    def test_new_currency_fails_explicitly_instead_of_being_absorbed_by_previous_block(self):
        with self.assertRaisesRegex(ValueError,'Unsupported currency section'):
            source_sections(self.reader('JPY Promo Rates'))

    def test_new_display_tenor_requires_mapping_instead_of_silently_using_wrong_color(self):
        sgd,_,_,_=source_sections(self.reader(tenor='2M'))
        self.assertIn('2M',sgd)


if __name__=='__main__':unittest.main()

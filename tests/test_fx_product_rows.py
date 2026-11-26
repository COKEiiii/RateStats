import copy
import unittest
from market_rates.fx_product_rows import consolidate_usd, reuse_product_row, caption_identity, source_rows


def offer(**changes):
    row = dict(bank='ICBC', product_id='icbc-fx-usd', currency='USD', rate_type='promo',
               tenor_value=1, tenor_unit='M', audience='personal', channel='电子银行/柜台',
               amount_min='5000', amount_max='100000', min_inclusive=True, max_inclusive=False,
               amount_currency='USD', amount_is_equivalent=False, fresh_funds='unknown',
               conditions='电子银行500起存；柜台20,000起存；金额档位以本表上下界为准',
               rate_pct='3.65', rate_basis='annual_nominal', valid_from=None, valid_to=None,
               availability='available', input_row=4)
    row.update(changes)
    return row


class FxProductRowsTests(unittest.TestCase):
    def test_current_rainbow_rejects_stale_quote_below_short_currency_group(self):
        from market_rates.fx_promo_plan import rainbow_public_cells
        from scripts.check_fx_promo import check_rainbow_public
        from market_rates.report_xml import address
        plan=dict(as_of='2026-10-22',usd_delta=0,cny_delta=0,extra_titles=[dict(row=131,currency='CAD')],rainbow_blocks=[dict(currency='CAD',tenor='7D',col=1,start=133,end=133,
            groups=[dict(bank='BEA',target_row=133,rows=[dict(formula='=A1',expected=.0157,amount='CAD ≥100,000；personal')])])])
        values={address(r,c):v for (r,c),v in rainbow_public_cells(plan).items()}
        self.assertEqual(check_rainbow_public(plan,values)['quote_rows'],1)
        for address_,value in [('B134',.021),('C134','EUR ≥100,000'),('A135','CITI')]:
            with self.subTest(cell=address_):
                with self.assertRaisesRegex(AssertionError,'quote identity'):check_rainbow_public(plan,dict(values,**{address_:value}))

    def test_exclusive_products_stay_with_bank_ranked_by_its_personal_winner(self):
        from market_rates.fx_promo_plan import rank_public_groups
        records=[offer(bank='HSBC',product_id='personal',rate_pct='3.4'),
                 offer(bank='HSBC',product_id='elite',audience='premier_elite',rate_pct='4.0'),
                 offer(bank='RHB',product_id='retail',rate_pct='3.2')]
        groups=[dict(bank=r['bank'],product_id=r['product_id'],order=1,rank=-1) for r in records]
        rank_public_groups(groups,records,'USD','1M')
        self.assertEqual([(g['product_id'],g['rank']) for g in groups],[('personal',3.4),('elite',3.4),('retail',3.2)])

    def test_unmatched_history_does_not_invent_quotes_on_spacer_rows(self):
        from market_rates.fx_promo_plan import unmatched_caption
        for value in (None,'','   '):self.assertEqual(unmatched_caption(value),'')
        self.assertEqual(unmatched_caption('普通个人存款'),'本期未列此旧产品或档位')
        self.assertIn('/ unknown',unmatched_caption('Priority Private Banking (USD 25K)'))

    def test_adjacent_same_quote_unions_bounds_and_keeps_sources(self):
        a=offer(); b=offer(amount_min='100000',amount_max=None,input_row=5)
        snapshot=copy.deepcopy([a,b]); rows,audit=consolidate_usd([a,b])
        self.assertEqual([a,b],snapshot)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['amount_min'],rows[0]['amount_max']),('5000',None))
        self.assertEqual([r['input_row'] for r in source_rows(rows[0])],[4,5])
        self.assertEqual(audit[0]['input_rows'],[4,5])

    def test_different_eligibility_rate_or_period_never_merges(self):
        for field,value in [('product_id','citi-fx-conversion'),('audience','premier'),('channel','counter'),
                            ('amount_currency','SGD'),('amount_is_equivalent',True),('fresh_funds','yes'),
                            ('conditions','仅限新客户'),('rate_pct','3.7'),('valid_to','2026-10-31'),
                            ('valid_from','2026-10-01'),('tenor_value',3),('rate_basis','effective')]:
            b=offer(amount_min='100000',amount_max=None,input_row=5,**{field:value})
            self.assertEqual(len(consolidate_usd([offer(),b])[0]),2,field)

    def test_gap_and_double_excluded_boundary_do_not_merge(self):
        b=offer(amount_min='100001',amount_max=None,input_row=5)
        self.assertEqual(len(consolidate_usd([offer(),b])[0]),2)
        b=offer(amount_min='100000',amount_max=None,input_row=5,min_inclusive=False)
        self.assertEqual(len(consolidate_usd([offer(),b])[0]),2)

    def test_inclusive_boundary_on_either_side_is_contiguous(self):
        b=offer(amount_min='100000',amount_max='500000',input_row=5,min_inclusive=False,max_inclusive=True)
        rows,_=consolidate_usd([offer(max_inclusive=True),b])
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['amount_max'],rows[0]['max_inclusive']),('500000',True))

    def test_unknown_minimum_not_assumed_zero(self):
        self.assertEqual(len(consolidate_usd([offer(amount_min=None),offer(amount_min='100000',input_row=5)])[0]),2)

    def test_other_currency_outside_requested_merge(self):
        self.assertEqual(len(consolidate_usd([offer(currency='CNY'),offer(currency='CNY',amount_min='100000',amount_max=None,input_row=5)])[0]),2)

    def test_exact_duplicate_retains_both_evidence_rows(self):
        rows,_=consolidate_usd([offer(),offer(input_row=5)])
        self.assertEqual(len(rows),1)
        self.assertEqual(len(source_rows(rows[0])),2)

    def test_legacy_ordinary_bank_product_rows_are_reused(self):
        cases=[('BOC','2k以上','2000',None),('BOC','4.3%(2k-200k)','2000','200000'),
               ('CIMB','>USD 10k','10000','1000000'),('RHB','≥USD5,000','5000',None),
               ('BEA','100k及以上','100000',None),('SBI','≥USD25,000','25000','1000000'),
               ('HSBC','USD30K','30000',None),('CITI','USD50k-5mil','50000','5000000')]
        for bank,label,lo,hi in cases:
            row=offer(bank=bank,amount_min=lo,amount_max=hi,product_id='citi-fx-conversion' if bank=='CITI' else 'ordinary')
            self.assertEqual(reuse_product_row(row,{9:label},'new full caption')[0],9,(bank,label))

    def test_new_amount_tier_cannot_overwrite_older_tier(self):
        labels={10:'<USD 5k',11:'>=USD 5k'}
        low=offer(amount_min='500',amount_max='5000')
        self.assertEqual(reuse_product_row(low,labels,'new')[0],10)
        self.assertEqual(reuse_product_row(offer(),labels,'new')[0],11)
        high=offer(amount_min='100000',amount_max=None)
        self.assertIsNone(reuse_product_row(high,labels,'new')[0])

    def test_one_history_row_cannot_be_assigned_twice(self):
        row=offer(bank='HSBC',amount_min='30000',amount_max=None)
        self.assertEqual(reuse_product_row(row,{9:'USD30K'},'new')[0],9)
        self.assertIsNone(reuse_product_row(row,{9:'USD30K'},'new',{9})[0])
        for audience in ['premier','premier_elite','premier_wealth','premier_standard']:
            self.assertIsNone(reuse_product_row(dict(row,audience=audience),{9:'USD30K'},'new')[0])

    def test_citi_conversion_and_wealth_and_new_funds_stay_distinct(self):
        row=offer(bank='CITI',product_id='citi-fx-new-funds',amount_min='5000',amount_max='5000000')
        self.assertIsNone(reuse_product_row(row,{4:'USD50k-5mil',5:'USD 200k to 5 Mil'},'new')[0])
        self.assertIsNone(reuse_product_row(dict(row,amount_min='10000'),{4:'USD 10k to <10 Mil Citigold/private'},'new')[0])

    def test_scb_customer_labels_do_not_collide(self):
        labels={5:'Personal Banking (USD 25K)',6:'Priority Banking (USD 25K)',7:'Priority Private Banking (USD 25K)'}
        for audience,index in [('personal',5),('premier',6),('private',7)]:
            row=offer(bank='SCB',audience=audience,amount_min='25000',amount_max=None)
            self.assertEqual(reuse_product_row(row,labels,'new')[0],index)

    def test_publication_caption_changes_are_not_new_products(self):
        old='外币定存促销 / personal\nUSD ≥5,000；online\n新资金；公告期 2026-09-01–2026-09-30（最新公开参考，公告已截止）'
        new='外币定存促销 / personal\nUSD ≥5,000；online\n新资金；公告期 2026-10-01–2026-10-31（最新公开参考，公告已截止）'
        self.assertEqual(caption_identity(old),caption_identity(new))
        self.assertEqual(reuse_product_row(offer(),{8:old},new)[0],8)

    def test_material_caption_eligibility_change_is_not_exact_duplicate(self):
        old='外币定存促销 / personal\nUSD ≥5,000；online\n仅限新客户'
        new=old.replace('仅限新客户','所有客户')
        self.assertIsNone(reuse_product_row(offer(),{8:old},new)[0])

    def test_duplicate_full_caption_reuses_first_without_a_third_row(self):
        label='外币定存促销 / personal\nUSD ≥5,000；online\n新资金'
        row,reason=reuse_product_row(offer(),{11:label,10:label},label)
        self.assertEqual((row,reason),(10,'duplicate_caption_first_row'))
        self.assertEqual(reuse_product_row(offer(),{11:label,10:label},label,{10})[0],11)

    def test_full_caption_wins_and_old_shorthand_is_not_current_twice(self):
        label='外币定存促销 / personal\nUSD ≥5,000 且<100,000；电子银行/柜台\n按金额档位适用'
        self.assertEqual(reuse_product_row(offer(),{10:'>=USD 5k',11:label},label)[0],11)

    def test_ambiguous_legacy_does_not_arbitrarily_overwrite(self):
        self.assertIsNone(reuse_product_row(offer(),{8:'>=USD 5k',9:'USD5K'},'new')[0])


if __name__=='__main__':
    unittest.main()

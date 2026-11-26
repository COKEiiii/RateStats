from decimal import Decimal as D
from unittest import TestCase

from market_rates.report_format import cny_promo_groups, hsbc_personal_groups, usd_board_groups


def record(row, label, **rates):
    return dict(row=row,label=label,rates={int(c[1:]):D(v) for c,v in rates.items()})


class BusinessLayoutTests(TestCase):
    def setUp(self):
        self.audit={'retained_amount_conflicts':[]}

    def test_usd_equivalent_uob_band_merges_but_narrower_range_does_not(self):
        rows=[record(4,'50k以下',c5='.0237'),
              record(5,'50k-100k',c5='.0237'),
              record(6,'定期存款挂牌；USD ≥25,000 且<50,000；personal',c4='.0238'),
              record(7,'定期存款挂牌；USD ≥50,000 且≤99,999；personal',c4='.0238')]
        result=usd_board_groups(rows,4,'UOB',self.audit)
        self.assertEqual(len(result),3)
        self.assertEqual(result[0],rows[0])
        self.assertEqual(result[1]['rates'],{4:D('.0238'),5:D('.0237')})
        self.assertEqual(result[2],rows[2])

    def test_usd_conflict_and_distinct_customer_tiers_stay_separate(self):
        rows=[record(4,'50k-100k',c5='.0237'),
              record(5,'定期存款挂牌；USD ≥50,000 且≤99,999；personal',c4='.0238',c5='.024')]
        self.assertEqual(usd_board_groups(rows,4,'UOB',self.audit),rows)
        self.assertEqual(len(self.audit['retained_amount_conflicts']),1)
        rows.append(record(6,'定期存款挂牌；USD ≥50,000 且≤99,999；premier',c4='.025'))
        self.assertEqual(usd_board_groups(rows,4,'UOB',self.audit),rows)

    def test_cny_split_preserves_two_distinct_historical_rates(self):
        def caption(amount,who='personal',note='普通新存单；须已有活期账户'):
            return f'手机银行普通促销 / {who}\nCNY {amount}；手机银行\n{note}'
        rows=[record(38,caption('≥3,000 且<50,000'),c4='.0145'),
              record(39,caption('≥50,000'),c4='.0155'),
              record(43,caption('≥3,000'),c3='.0145')]
        result=cny_promo_groups(rows,3,'BOC',self.audit)
        self.assertEqual([r['label'] for r in result],['CNY ≥3,000 且<50,000','CNY ≥50,000'])
        self.assertEqual([r['rates'][3]-r['rates'][4] for r in result],[D(0),D('-.001')])
        self.assertEqual({r['row'] for r in result},{43})
        self.assertTrue(all(r['extra_note']=='本期官网金额：CNY ≥3,000' for r in result))
        rows[2]=record(43,caption('≥3,000',who='premier'),c3='.0145')
        self.assertEqual(cny_promo_groups(rows,3,'BOC',self.audit),rows)
        rows[2]=record(43,caption('≥3,000',note='限新客户'),c3='.0145')
        self.assertEqual(cny_promo_groups(rows,3,'BOC',self.audit),rows)

    def test_cny_boundary_conflict_does_not_hide_either_quote(self):
        head='手机银行普通促销 / personal\nCNY '
        tail='；手机银行\n普通新存单'
        rows=[record(8,head+'≥3,000 且≤50,000'+tail,c4='.0145'),
              record(9,head+'≥50,000'+tail,c4='.0155'),
              record(10,head+'≥3,000'+tail,c3='.0145')]
        self.assertEqual(cny_promo_groups(rows,3,'BOC',self.audit),rows)
        self.assertEqual(len(self.audit['retained_amount_conflicts']),1)

    def test_hsbc_exclusive_customer_rates_do_not_enter_main_table(self):
        label='外币定存促销 / {}\nUSD ≥30,000；柜台\n须新资金'
        rows=[record(25,label.format('personal'),c4='.0385',c5='.0385'),
              record(26,label.format('premier_elite'),c4='.046'),
              record(27,label.format('premier_standard'),c4='.039'),
              record(28,label.format('premier_wealth'),c4='.044')]
        self.assertEqual(hsbc_personal_groups(rows,4,self.audit),rows[:1])
        self.assertEqual([r['row'] for r in self.audit['hsbc_usd_archived_rows']],[26,27,28])

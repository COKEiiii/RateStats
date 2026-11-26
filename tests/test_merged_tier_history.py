"""Merged amounts compare only with a complete equal-rate eligible history."""
from decimal import Decimal as D
from unittest import TestCase

from market_rates.report_format import match_previous


def caption(amount,*,product='外币定存促销',audience='personal',currency='USD',channel='电子银行/柜台',
            condition='电子银行500起存；柜台20,000起存；金额档位以本表上下界为准'):
    return f'{product} / {audience}\n{currency} {amount}；{channel}\n{condition}'


class MergedTierHistoryTests(TestCase):
    def match(self,rows,current=None):
        return match_previous(current or caption('≥5,000'),'ICBC',rows,'USD')

    def test_equal_old_tiers_cover_current_open_range(self):
        rows=[(caption('≥5,000 且<100,000'),D('.0365')),(caption('≥100,000'),D('.0365'))]
        self.assertEqual(self.match(rows),D('.0365'))
        self.assertEqual(self.match(list(reversed(rows))),D('.0365'))

    def test_different_prior_rates_do_not_become_one_rate(self):
        rows=[(caption('≥5,000 且<100,000'),D('.0365')),(caption('≥100,000'),D('.037'))]
        self.assertEqual(self.match(rows),'-')

    def test_internal_gap_and_double_excluded_boundary_are_not_covered(self):
        for upper,lower in [('≥5,000 且<100,000','≥100,001'),('≥5,000 且<100,000','>100,000')]:
            self.assertEqual(self.match([(caption(upper),D('.0365')),(caption(lower),D('.0365'))]),'-')

    def test_shared_boundary_can_be_included_on_either_side(self):
        rows=[(caption('≥5,000 且≤100,000'),D('.0365')),(caption('>100,000'),D('.0365'))]
        self.assertEqual(self.match(rows),D('.0365'))

    def test_missing_lower_or_open_upper_coverage_is_not_guessed(self):
        pairs=[('≥5,001 且<100,000','≥100,000'),
               ('≥5,000 且<100,000','≥100,000 且≤1,000,000'),
               ('<100,000','≥100,000')]
        for lower,upper in pairs:
            self.assertEqual(self.match([(caption(lower),D('.0365')),(caption(upper),D('.0365'))]),'-')

    def test_distinct_product_customer_channel_qualification_or_currency_does_not_fill_gap(self):
        for changes in [dict(product='换汇定存促销'),dict(audience='premier'),dict(channel='柜台'),
                        dict(condition='仅限新客户'),dict(currency='SGD'),dict(currency='USD 等值')]:
            rows=[(caption('≥5,000 且<100,000'),D('.0365')),(caption('≥100,000',**changes),D('.0365'))]
            self.assertEqual(self.match(rows),'-',changes)

    def test_unknown_legacy_product_identity_cannot_join_amounts(self):
        rows=[('USD ≥5,000 且<100,000',D('.0365')),('USD ≥100,000',D('.0365'))]
        self.assertEqual(self.match(rows),'-')

    def test_three_tiers_cover_bounded_target_and_preserve_upper_inclusion(self):
        rows=[(caption('≥5,000 且<50,000'),D('.0365')),(caption('≥50,000 且<100,000'),D('.0365')),
              (caption('≥100,000 且<200,000'),D('.0365'))]
        self.assertEqual(self.match(rows,caption('≥5,000 且<200,000')),D('.0365'))
        self.assertEqual(self.match(rows,caption('≥5,000 且≤200,000')),'-')

    def test_only_intersecting_tiers_need_the_same_rate(self):
        rows=[(caption('≥500 且<5,000'),D('.031')),(caption('≥5,000 且<100,000'),D('.0365')),
              (caption('≥100,000'),D('.0365'))]
        self.assertEqual(self.match(rows),D('.0365'))

    def test_overlap_with_conflicting_rate_is_not_ignored(self):
        rows=[(caption('≥5,000 且<100,000'),D('.0365')),(caption('≥100,000'),D('.0365')),
              (caption('≥50,000 且<150,000'),D('.038'))]
        self.assertEqual(self.match(rows),'-')

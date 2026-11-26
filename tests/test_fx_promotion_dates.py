import unittest

from market_rates.fx_promo import normalize_fx, promotion_dates


class PromotionStartsOnTests(unittest.TestCase):
    def section(self, quote):
        return dict(id='hsbc-usd-personal', bank='HSBC', currency='USD',
                    product_id='hsbc-fx-usd-personal', layout='fx-promo-usd-personal',
                    page_id='hsbc-usd', rows=[['Tenure', 'Promotional rate'],
                                             ['3-month', '3.50% p.a.']],
                    extra_evidence=[dict(page_id='hsbc-usd-terms', quote=quote,
                                         locator='promotion-period')])

    def test_starts_on_clause_survives_pdf_line_breaks(self):
        section = self.section('This Promotion is offered by HSBC. The Promotion starts on \n'
                               '1 November 2026 and ends on 30 November 2026 '
                               '(the “Promotional Period”).')
        self.assertEqual(promotion_dates(section), ('2026-11-01', '2026-11-30'))
        rows = normalize_fx(section)
        self.assertEqual([(r['valid_from'], r['valid_to']) for r in rows],
                         [('2026-11-01', '2026-11-30')])

    def test_dates_follow_new_source_month(self):
        section = self.section('The Promotional Period starts on 2 December 2026 '
                               'and ends on 31 December 2026.')
        self.assertEqual(promotion_dates(section), ('2026-12-02', '2026-12-31'))

    def test_unrelated_start_dates_do_not_supply_promotion_validity(self):
        for quote in ['', 'Promotion details updated 1 November 2026.',
                      'For this promotion, account membership starts on 1 November 2026.']:
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, 'current evidence'):
                promotion_dates(self.section(quote))


if __name__ == '__main__':
    unittest.main()

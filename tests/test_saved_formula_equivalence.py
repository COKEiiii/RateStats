import unittest

from scripts.check_three_bank_validation import same_formula


class SavedFormulaEquivalenceTests(unittest.TestCase):
    def test_excel_optional_sheet_quotes(self):
        self.assertTrue(same_formula("=SUM('本期专属明细'!$J$4:$J$35)", "=SUM(本期专属明细!$J$4:$J$35)"))

    def test_changed_reference_and_literal_remain_different(self):
        self.assertFalse(same_formula("='本期专属明细'!J4", "=本期专属明细!J5"))
        self.assertFalse(same_formula('=IF(A1="\'明细\'!J4",1,0)', '=IF(A1="明细!J4",1,0)'))
        self.assertFalse(same_formula("='旧明细'!J4", "=新明细!J4"))


if __name__ == '__main__':
    unittest.main()

"""Dynamic table screenshots may race with a bank page redraw."""
import unittest
from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import Error as PlaywrightError

from market_rates.evidence_capture import table_overview


class TableOverviewRetryTests(unittest.TestCase):
    def test_redrawn_table_is_relocated_and_captured(self):
        page=Mock();table=Mock()
        table.screenshot.side_effect=[PlaywrightError('Element is not attached to the DOM'),None]
        table_overview(page,table,Path('overview.png'))
        self.assertEqual(table.wait_for.call_count,2)
        self.assertEqual(table.screenshot.call_count,2)
        self.assertEqual(page.evaluate.call_count,2)
        page.wait_for_timeout.assert_called_once_with(300)

    def test_unrelated_screenshot_error_is_not_retried(self):
        page=Mock();table=Mock()
        table.screenshot.side_effect=PlaywrightError('Permission denied')
        with self.assertRaisesRegex(PlaywrightError,'Permission denied'):
            table_overview(page,table,Path('overview.png'))
        self.assertEqual(table.screenshot.call_count,1)
        page.wait_for_timeout.assert_not_called()


if __name__=='__main__':unittest.main()

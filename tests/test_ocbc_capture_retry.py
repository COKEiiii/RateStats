from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scripts.capture_ocbc_board import reveal_interest_section


class OcbcTermsRetryTests(TestCase):
    def test_reloads_incomplete_terms_page_before_clicking_interest(self):
        page=Mock()
        title=page.locator.return_value
        title.wait_for.side_effect=[PlaywrightTimeoutError('missing'),None]
        page.reload.return_value.status=200
        with TemporaryDirectory() as folder:
            reveal_interest_section(page,Path(folder))
        page.reload.assert_called_once()
        title.click.assert_called_once()

    def test_preserves_last_html_when_interest_never_appears(self):
        page=Mock()
        page.locator.return_value.wait_for.side_effect=PlaywrightTimeoutError('missing')
        page.reload.return_value.status=200
        page.content.return_value='<html>source unavailable</html>'
        with TemporaryDirectory() as folder:
            with self.assertRaises(PlaywrightTimeoutError):
                reveal_interest_section(page,Path(folder))
            self.assertEqual((Path(folder)/'ocbc-terms-failed.html').read_text(),
                             '<html>source unavailable</html>')
        self.assertEqual(page.reload.call_count,2)

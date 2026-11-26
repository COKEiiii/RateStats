from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as BrowserTimeout

from market_rates.board_capture import BoardCapture
from scripts.capture_board_repairs import open_bea_board
from scripts.run_board_bulk import archive_interrupted_icbc_hlf


class BoardCaptureRetryTests(TestCase):
    url = 'https://www.hlf.com.sg/personal/deposits/fixed-deposits-singapore.php'

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.capture = BoardCapture(self.root / 'capture')
        self.context = Mock()

    def page(self, *, status=None, error=None):
        page = Mock(url=self.url)
        if error is not None:
            page.goto.side_effect = error
        else:
            page.goto.return_value = Mock(status=status)
        return page

    @patch('market_rates.board_capture.sleep')
    def test_timeout_and_503_retry_with_new_pages(self, sleep):
        pages = [self.page(error=BrowserTimeout('timeout')), self.page(status=503), self.page(status=200)]
        self.context.new_page.side_effect = pages
        page, evidence = self.capture.open(self.context, 'HLF', 'sgd', self.url, attempts=3)
        self.assertIs(page, pages[2])
        self.assertEqual(evidence['url'], self.url)
        pages[0].close.assert_called_once()
        pages[1].close.assert_called_once()
        pages[2].close.assert_not_called()
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,)])

    @patch('market_rates.board_capture.sleep')
    def test_404_fails_immediately_with_status(self, sleep):
        page = self.page(status=404)
        self.context.new_page.return_value = page
        with self.assertRaisesRegex(ValueError, 'HTTP 404'):
            self.capture.open(self.context, 'HLF', 'sgd', self.url, attempts=3)
        self.context.new_page.assert_called_once()
        page.close.assert_called_once()
        sleep.assert_not_called()

    @patch('market_rates.board_capture.sleep')
    def test_persistent_503_does_not_return_evidence(self, sleep):
        pages = [self.page(status=503) for _ in range(3)]
        self.context.new_page.side_effect = pages
        with self.assertRaisesRegex(ValueError, 'HTTP 503'):
            self.capture.open(self.context, 'HLF', 'sgd', self.url, attempts=3)
        self.assertEqual(self.context.new_page.call_count, 3)
        for page in pages:
            page.close.assert_called_once()

    def test_interrupted_evidence_is_archived_and_writer_can_open_same_path(self):
        work = self.root / 'board'
        dest = work / 'icbc-hlf'
        failed = BoardCapture(dest)
        evidence = failed.ev / 'saved.html'
        evidence.write_bytes(b'original evidence')
        with self.assertRaises(ValueError):
            archive_interrupted_icbc_hlf(self.capture.out, work)
        archived = archive_interrupted_icbc_hlf(dest, work)
        self.assertEqual((archived / 'evidence' / evidence.name).read_bytes(), b'original evidence')
        self.assertFalse(dest.exists())
        self.assertTrue(BoardCapture(dest).ev.is_dir())

    def test_bea_error_page_retries_registered_entry_in_a_new_page(self):
        entry = 'https://www.hkbea.com.sg/html/en/beasg-rates-sgd-fixed-deposit-rates.html'
        failed = Mock(url='https://www.hkbea.com.sg/html/error/error.html')
        good = Mock(url='https://www.hkbea.com.sg/sg-form/?formId=RATE&rateType=fdr#/forms/MISC')
        for page in (failed, good):
            page.goto.return_value.status = 200
        self.context.new_page.side_effect = [failed, good]

        self.assertIs(open_bea_board(self.context, entry), good)

        failed.close.assert_called_once()
        good.close.assert_not_called()
        self.assertEqual([page.goto.call_args.args[0] for page in (failed, good)], [entry, entry])

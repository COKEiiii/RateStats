"""Real Chromium checks for the scoped HTML evidence used by live collection."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_rates.capture import _html_evidence


class HtmlEvidenceTest(unittest.TestCase):
    def test_unit_cutoff_and_shared_image(self):
        from playwright.sync_api import sync_playwright

        with TemporaryDirectory() as folder, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 700})
                page.set_content('''<table id="rates"><tr><th>Tenor</th><th>Rate</th></tr>
                    <tr><td>SGD 6M</td><td>1.75%</td></tr>
                    <tr><td>USD 6M</td><td>3.00%</td></tr></table>''')
                unit = dict(selector="#rates", end_before='tr:has-text("USD")',
                            kind="rates", product_ids=["sgd"])
                body, images, texts, mapped, _ = _html_evidence(
                    page, {"capture_units": [unit, unit]}, Path(folder), "cut", 4)
                self.assertIn("SGD 6M", body)
                self.assertNotIn("USD 6M", body)
                self.assertEqual(len(images), 1)
                self.assertEqual(mapped, [images[0], images[0]])
                self.assertTrue((Path(folder) / images[0]).stat().st_size > 0)
            finally:
                browser.close()

    def test_standalone_section_excludes_bundle(self):
        from playwright.sync_api import sync_playwright

        with TemporaryDirectory() as folder, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 700})
                page.set_content('''<body><h1>SGD Time Deposit Promotion</h1>
                    <div>i. Bundle Promotion</div><table><tr><td>Bundle</td><td>9.99%</td></tr></table>
                    <h2>ii. Standalone Time Deposit</h2>
                    <table><tr><th>Tenure</th><th>Promotional Rates</th></tr>
                    <tr><td>6M</td><td>1.50%</td></tr></table>
                    <h2>How to place deposit</h2></body>''')
                section = dict(start=r"^ii\.\s*Standalone[^\n]*$", end=r"^How to place deposit$",
                               table_headers=["Tenure", "Promotional Rates"])
                body, images, _, _, _ = _html_evidence(page,
                    {"capture_units": [dict(selector="body", section_between=section)]},
                    Path(folder), "section", 4)
                self.assertIn("1.50%", body)
                self.assertNotIn("9.99%", body)
                self.assertEqual(len(images), 1)
                self.assertTrue((Path(folder) / images[0]).stat().st_size > 0)
            finally:
                browser.close()

    def test_wide_table_is_split_with_all_tenors_and_shared_evidence(self):
        from playwright.sync_api import sync_playwright

        with TemporaryDirectory() as folder, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 700})
                page.set_content('''<table id="rates"><tr><th>SGD</th><th>Promotion Rates</th></tr>
                  <tr><th>Tenor</th><th>SGD500 to SGD20K</th><th>SGD20K and Above</th></tr>
                  <tr><td>1 month</td><td>1.05%</td><td>1.10%</td></tr>
                  <tr><td>3 months</td><td>1.25%</td><td>1.30%</td></tr>
                  <tr><td>6 months</td><td>1.30%</td><td>1.80%</td></tr>
                  <tr><td>9 months</td><td>1.10%</td><td>1.80%</td></tr>
                  <tr><td>1 year</td><td>1.15%</td><td>1.85%</td></tr></table>''')
                unit = dict(selector="#rates", split_table_rows=2, kind="rates", product_ids=["sgd"])
                _, images, texts, mapped, expanded = _html_evidence(
                    page, {"capture_units": [unit]}, Path(folder), "split", 4)
                self.assertEqual(len(texts), 3)
                self.assertEqual(len(images), 1)
                self.assertEqual(mapped, [images[0]] * 3)
                self.assertEqual([u["table_row_range"] for u in expanded], [[2, 4], [4, 6], [6, 7]])
                self.assertEqual(sum(text.count("month") + text.count("year") for text in texts), 5)
                self.assertTrue(all("SGD500 to SGD20K" in text for text in texts))
            finally:
                browser.close()

    def test_cutoff_image_below_viewport(self):
        from playwright.sync_api import sync_playwright

        with TemporaryDirectory() as folder, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 800, "height": 600})
                page.set_content('''<div style="height:2500px">Earlier content</div>
                    <table id="rates"><tr><td>SGD</td><td>1.75%</td></tr>
                    <tr><td>USD</td><td>3.00%</td></tr></table>''')
                body, images, _, _, _ = _html_evidence(page,
                    {"capture_units": [dict(selector="#rates", end_before='tr:has-text("USD")')]},
                    Path(folder), "deep", 4)
                self.assertIn("1.75%", body)
                self.assertNotIn("3.00%", body)
                self.assertTrue((Path(folder) / images[0]).stat().st_size > 0)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()

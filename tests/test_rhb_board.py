import copy
import tempfile
import unittest
from pathlib import Path

import pymupdf

from market_rates.board_capture import BoardCapture
from market_rates.board_wave import normalize_section, validate_wave
from market_rates.board_wave import wave_source_hash
from market_rates.common import digest, save
from market_rates.rhb_board import capture_sgd_pdf, corporate_tenors, parse_sgd_grid


class RhbBoardTests(unittest.TestCase):
    def grid(self):
        return [
            ['RHB BANK DEPOSIT RATES', None, None, None, None],
            ['Deposit Amount', '1-mth*', '2-mth*', '3-mth', None],
            ['$20,000 to $200,000', '0.1000', '0.1500', '0.3000', ''],
            ['$200,000 & above', '0.1000', '0.1500', '0.3000', ''],
            ['Foreign Currency Call A/C Rates (% p.a.)', None],
            ['Currency', 'Interest Rates'],
        ]

    def test_sgd_matrix_excludes_adjacent_fx_call_account(self):
        rows, indices = parse_sgd_grid(self.grid())
        self.assertEqual(indices, [1, 2, 3])
        self.assertEqual(len(rows[0]), 4)
        self.assertEqual(rows[-1][-1], '0.3000')

    def test_added_amount_tier_and_tenor_are_preserved(self):
        grid = self.grid()
        for row in grid[1:4]:
            row.insert(-1, '48-mth' if row[0] == 'Deposit Amount' else '0.7000')
        grid.insert(4, ['$500,000 & above', '0.1000', '0.1500', '0.5000', '0.9000', None])
        rows, _ = parse_sgd_grid(grid)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][-1], '48-mth')
        self.assertEqual(rows[-1][-1], '0.9000')

    def test_deleted_amount_tier_is_not_recreated(self):
        grid = self.grid()
        del grid[3]
        self.assertEqual(len(parse_sgd_grid(grid)[0]), 2)

    def test_missing_rate_never_shifts_columns(self):
        grid = self.grid()
        grid[2][2] = None
        with self.assertRaisesRegex(ValueError, 'missing cells'):
            parse_sgd_grid(grid)

    def test_duplicate_header_is_rejected(self):
        grid = self.grid()
        grid.insert(0, copy.deepcopy(grid[1]))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            parse_sgd_grid(grid)

    def test_zero_is_a_numeric_quote(self):
        grid = self.grid()
        grid[2][3] = '0.0000'
        rows, _ = parse_sgd_grid(grid)
        section = dict(id='rhb-sgd', page_id='p', bank='RHB', currency='SGD', product_id='rhb-sgd-board',
                       table=0, layout='rhb-sgd', rows=rows, corporate_only_tenors=[1, 2])
        quotes = normalize_section(section, 'llm')
        self.assertEqual(quotes[2]['rate_pct'], '0')
        self.assertEqual(quotes[2]['audience'], 'personal')
        self.assertEqual([q['audience'] for q in quotes[:2]], ['corporate', 'corporate'])

    def test_eligibility_is_read_from_current_starred_note(self):
        note = '*Note: Rates for 2-mth are applicable for Commercial and Corporate customers only'
        self.assertEqual(corporate_tenors(note, ['1-mth', '2-mth*', '3-mth']), [2])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            corporate_tenors(note, ['1-mth*', '2-mth*', '3-mth'])

    def pdf(self):
        # Put the table on page 2 and well away from the historical fixed crop.
        doc = pymupdf.open()
        doc.new_page()
        page = doc.new_page(width=900, height=1000)
        for y, value in [(280, 'RHB BANK DEPOSIT RATES'), (300, '8-Oct-26'),
                         (320, 'SGD Fixed Deposit Rates (% p.a.)')]:
            page.insert_text((70, y), value, fontsize=10)
        xs, ys = [70, 300, 410, 520, 630], [340, 370, 400, 430]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for y, row in zip(ys, self.grid()[1:4]):
            for x, value in zip(xs, row):
                if value:
                    page.insert_text((x + 3, y + 19), value, fontsize=10)
        page.insert_text((70, 450), '*Note: Rates for 1-mth and 2-mth are applicable for Commercial and Corporate customers only', fontsize=9)
        page.insert_text((70, 470), '^Note: For balances below $20,000, interest is accorded at 0.00%', fontsize=9)
        return doc

    def test_old_fx_only_capture_is_not_reused_as_complete_rhb(self):
        from scripts.capture_remaining_board import capture_is_reusable
        old = dict(sections=[dict(layout='rhb-fx')])
        self.assertFalse(capture_is_reusable('RHB', old))
        self.assertTrue(capture_is_reusable('UOB', old))
        current = dict(sections=[dict(layout='rhb-fx'), dict(layout='rhb-sgd')], source_file_hashes={'source.pdf': 'hash'})
        self.assertTrue(capture_is_reusable('RHB', current))

    def test_capture_finds_repositioned_page_and_retains_pdf_integrity(self):
        with tempfile.TemporaryDirectory() as tmp, self.pdf() as doc:
            batch = BoardCapture(Path(tmp) / 'capture')
            section = capture_sgd_pdf(batch, doc, 'https://example.invalid/official.pdf')
            pdf = batch.ev / 'source.pdf'
            doc.save(pdf)
            run = batch.save()
            self.assertEqual(section['source_date'], '2026-10-08')
            self.assertTrue(section['page_id'].endswith('sgd-p2'))
            self.assertEqual(len(section['task_ids']), 7)
            images={name for task in run['tasks'] for name in task['images']}
            self.assertEqual(len(images),1)  # All seven text tasks share the PDF page.
            self.assertEqual(len(list(batch.ev.glob('*.png'))),1)
            self.assertFalse(any('source_cell_cuts' in t for t in run['tasks']))
            self.assertIn('source.pdf', run['source_file_hashes'])
            save(batch.out / 'wave-checks.json', dict(passed=True, extraction_mode='llm_only',source_hash=wave_source_hash(run), task_hash=run['task_hash'],
                 checks=[dict(task=t['id'], lane=lane, task_hash=digest(t), passed=True)
                         for t in run['tasks'] for lane in ['llm']]))
            validate_wave(batch.out)
            pdf.write_bytes(pdf.read_bytes() + b'changed')
            with self.assertRaisesRegex(ValueError, 'Original PDF changed'):
                validate_wave(batch.out)


if __name__ == '__main__':
    unittest.main()

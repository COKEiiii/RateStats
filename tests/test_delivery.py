"""Publication should be all-or-nothing and safe to resume."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_rates.delivery import find_stale_sgd_quotes, publish_four, verify_delivery


class DeliveryTests(TestCase):
    def test_stale_sgd_quote_detection_does_not_change_source(self):
        from openpyxl import Workbook
        with TemporaryDirectory() as tmp:
            source,copy=(Path(tmp)/n for n in ('source.xlsx','copy.xlsx'))
            book=Workbook();sheet=book.active;sheet.title='SGD Promotional Rate'
            sheet['E21']='=明细_20261008!H78';sheet['F21']='旧日期报价'
            sheet['H20']='=明细_20261019!H22';sheet['I20']='本期报价'
            book.save(source)
            copy.write_bytes(source.read_bytes())
            removed=find_stale_sgd_quotes(copy,date(2026,10,19))
            self.assertEqual(removed,[{'cell':'E21','source_dates':['20261008']}])
            self.assertEqual(copy.read_bytes(),source.read_bytes())

    def test_four_files_resume_and_changed_input_rejected(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            report,rainbow,prior=(root/n for n in ('report.xlsx','rainbow.xlsx','prior.xlsx'))
            for p in (report,rainbow,prior):p.write_bytes(p.name.encode())
            templates={'report':report,'rainbow':rainbow}
            def formatter(source,template,output,**kwargs):
                Path(output).write_bytes(b'formatted-'+Path(source).read_bytes())
                return {'output':str(output),'validation':{'passed':True},'rates':[Decimal('0.0355')]}
            with patch('market_rates.rainbow_borders.fix_sgd_bank_borders'),patch('market_rates.delivery.Book'),patch('market_rates.delivery.infer_date',return_value=date(2026,10,8)), \
                 patch('market_rates.delivery.format_report',side_effect=formatter) as report_fn, \
                 patch('market_rates.delivery.format_rainbow',side_effect=formatter), \
                 patch('market_rates.delivery.validate_native_books',return_value={'passed':True}):
                output=root/'output'
                result=publish_four(report,rainbow,templates,output)
                self.assertEqual(len(list(output.glob('*.xlsx'))),4)
                self.assertEqual(Path(result['files']['raw_report']).read_bytes(),report.read_bytes())
                self.assertEqual(publish_four(report,rainbow,templates,output),result)
                self.assertEqual(report_fn.call_count,1)
                self.assertNotIn('previous_path',report_fn.call_args.kwargs)
                self.assertEqual(result['comparison_mode'],'latest_two_columns')
                self.assertNotIn('previous_sha256',result['inputs'])
                prior.write_bytes(b'changed historical comparison')
                self.assertEqual(publish_four(report,rainbow,templates,output),result)
                prior.unlink()
                self.assertEqual(publish_four(report,rainbow,templates,output),result)
                report.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'输入已改变'):
                    publish_four(report,rainbow,templates,output)
                Path(result['files']['formatted_report']).unlink()
                with self.assertRaisesRegex(ValueError,'缺失或被修改'):
                    verify_delivery(output)

    def test_failed_second_formatter_never_publishes_partial_delivery(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            report,rainbow=(root/n for n in ('report.xlsx','rainbow.xlsx'))
            for p in (report,rainbow):p.write_bytes(b'original')
            def first(source,template,output,**kwargs):
                Path(output).write_bytes(b'first output')
                return {}
            with patch('market_rates.rainbow_borders.fix_sgd_bank_borders'),patch('market_rates.delivery.Book'),patch('market_rates.delivery.infer_date',return_value=date(2026,10,8)), \
                 patch('market_rates.delivery.format_report',side_effect=first), \
                 patch('market_rates.delivery.format_rainbow',side_effect=ValueError('bad second input')):
                with self.assertRaisesRegex(ValueError,'bad second input'):
                    publish_four(report,rainbow,{'report':report,'rainbow':rainbow},root/'output')
                self.assertFalse((root/'output').exists())
                self.assertEqual(report.read_bytes(),b'original')

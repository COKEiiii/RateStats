import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.platform_runtime import canonical, excel_command, same_path, pdf_converter


class PlatformRuntimeTests(unittest.TestCase):
    def test_pdf_preview_converter_survives_finder_minimal_path(self):
        from market_rates.common import save
        with TemporaryDirectory() as temporary:
            root=Path(temporary);converter=root/'中文 转换器';converter.write_text('#!/bin/sh\n');converter.chmod(0o755)
            save(root/'config/excel.runtime.local.json',{'pdf_converter':str(converter)})
            with patch('scripts.platform_runtime.ROOT',root),patch.dict(os.environ,{'PATH':'/usr/bin:/bin'},clear=True):
                self.assertEqual(pdf_converter(),str(converter.resolve()))

    def test_canonical_paths_follow_symlink_aliases(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            alias = root.parent / (root.name + '-alias')
            try:
                alias.symlink_to(root, target_is_directory=True)
                self.assertTrue(same_path(root / 'a b.xlsx', alias / 'a b.xlsx'))
                self.assertEqual(canonical(root / '..' / root.name), canonical(alias))
            finally:
                alias.unlink(missing_ok=True)

    def test_macos_excel_dispatch_preserves_native_arguments(self):
        cases = {
            'sgd': ('-PlanPath', '中文 路径/plan.json'),
            'board-cleanup': ('-PlanPath', 'p.json', '-ReportOutput', 'out.xlsx'),
            'check-delivery': ('--Manifest', 'manifest.json', '--Out', 'checks.json'),
            'check-premier': ('--Manifest', 'manifest.json', '--Out', 'checks.json'),
            'style-check': ('--PlanPath', 'plan.json'),
            'weekly-preview': ('--PlanPath', 'plan.json'),
        }
        for operation, args in cases.items():
            with self.subTest(operation=operation):
                self.assertEqual(
                    excel_command(operation, *args),
                    [sys.executable, '-m', 'scripts.excel_native', operation, *args],
                )


if __name__ == '__main__':
    unittest.main()

import hashlib
from pathlib import Path
import tempfile
import unittest

from market_rates.common import save, load, digest
from market_rates.evidence_repair import repair
from market_rates.visible_html import visible_source_text


HEADING = '<strong>For Premier customers with wealth holdings or Premier FX Engaged customers<sup><a><span aria-hidden="true">3</span><span class="visuallyhidden">Footnote link 3</span></a></sup>:</strong>'
RAW = 'For Premier customers with wealth holdings or Premier FX Engaged customers3\nFootnote link 3\n:'
VISIBLE = 'For Premier customers with wealth holdings or Premier FX Engaged customers3:'


class VisibleHtmlTests(unittest.TestCase):
    def test_exact_source_hidden_hint_removed_and_visible_superscript_kept(self):
        self.assertEqual(visible_source_text(HEADING, RAW), dict(text=VISIBLE, removed=['Footnote link 3']))

    def test_visible_footnote_words_are_not_removed(self):
        self.assertIsNone(visible_source_text(HEADING.replace('class="visuallyhidden"', ''), RAW))

    def test_hidden_eligibility_and_rates_are_not_removed(self):
        value = '<strong>Rate <span class="visuallyhidden">3.5% for Premier only</span></strong>'
        self.assertIsNone(visible_source_text(value, 'Rate 3.5% for Premier only'))

    def test_other_source_does_not_silently_rewrite_expected(self):
        self.assertIsNone(visible_source_text(HEADING.replace('wealth', 'savings'), RAW))

    def test_duplicate_responsive_markup_with_same_visible_words_is_safe(self):
        self.assertEqual(visible_source_text(HEADING+HEADING, RAW)['text'], VISIBLE)

    def test_source_repair_preserves_pixels_and_archives_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ev=root/'evidence';ev.mkdir()
            png=b'original screenshot bytes';(ev/'heading.png').write_bytes(png)
            (ev/'source.html').write_text(HEADING, encoding='utf-8')
            task=dict(id='heading',bank='HSBC',page_id='source',kind='text',expected=RAW,images=['heading.png'],image_hashes={'heading.png':hashlib.sha256(png).hexdigest()})
            run=dict(tasks=[task],sections=[dict(extra_evidence=[dict(locator='heading',quote=RAW)])],pages=[],task_hash=digest([task]))
            save(root/'run.json',run);save(root/'wave-checks.json',dict(task_hash=digest([task]),errors=[dict(task='heading',lane='llm',error='Literal source/transcription mismatch')]))
            result=repair(root,'source');after=load(root/'run.json')
            self.assertEqual(len(result['actions']),1)
            self.assertEqual(after['tasks'][0]['expected'],RAW)
            self.assertEqual(after['tasks'][0]['text_chunk_size'],600)
            self.assertEqual((ev/'heading.png').read_bytes(),png)
            self.assertEqual(load(Path(result['archive'])/'run.json')['tasks'][0]['expected'],RAW)
            self.assertNotIn('passed',after['tasks'][0])
            self.assertEqual(after['sections'][0]['extra_evidence'][0]['quote'],RAW)


if __name__=='__main__':unittest.main()

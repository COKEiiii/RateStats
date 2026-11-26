"""Synthetic values deliberately differ from the frozen SCB benchmark."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_rates.extraction_contract import response_schema, parse_tenor, parse_audience, check_reference
from market_rates.scb_extractor import (bind_unit, bind_terms_text, combine, parse_rate, parse_money,
                                        parse_period, extract, TABLE_INSTRUCTIONS, PDF_INSTRUCTIONS)
from market_rates.clause_review import clause_differences


class LiteralExtractionTests(unittest.TestCase):
    def setUp(self):
        self.product = {"id": "scb-sgd-fresh-funds", "bank": "SCB", "name": "Synthetic deposit"}
        self.config = {"products": [self.product], "models": {"max_images": 3, "num_ctx": 32768}}
        self.web = {"id": "web-id", "bank": "SCB", "images": ["web-shot.png"], "text":
                    "9 months Personal Banking: 3.25% p.a. S$12,000 SGD Online Banking or SC Mobile "
                    "From 02 January 2027 to 31 January 2027"}
        self.pdf = {"id": "pdf-id", "bank": "SCB", "images": ["terms-p1.png", "terms-p2.png"],
                    "capture_scope": "pdf", "url": "https://example.invalid/singapore-dollar-time-deposit-tnc.pdf",
                    "text": "[PDF page 1]\n1. From 06 January 2027 to 31 January 2027.\n"
                    "2. Fresh funds are funds that do not originate here and are not re-deposited within 19 days.\n"
                    "3. Priority Banking status must be maintained at all times.\n[PDF page 2]\n4. Other terms apply."}
        self.unit = {"page": self.web, "text": self.web["text"], "label": "web-shot.png"}
        self.raw = {"rows": [{"product_id": self.product["id"], "tenor_text": "9 months",
                     "audience_text": "Personal Banking", "rate_text": "3.25% p.a.", "minimum_text": "S$12,000",
                     "minimum_inclusive": True, "maximum_text": None, "maximum_inclusive": False,
                     "currency_text": "SGD", "availability": "available", "locator": "table"}],
                    "channels": [{"product_id": self.product["id"], "quote": "Online Banking or SC Mobile", "locator": "heading"}],
                    "periods": [{"product_id": self.product["id"], "quote": "From 02 January 2027 to 31 January 2027", "locator": "heading"}],
                    "terms": [], "coverage_complete": True, "unreadable": []}

    def test_original_tenor_and_customer_identity(self):
        self.assertEqual(parse_tenor("9 months"), (9, "M"))
        self.assertEqual(parse_tenor("180 days"), (180, "D"))
        for label, expected in [("Personal Banking: 3.25% p.a.", "personal"), ("Priority Banking", "preferred"),
                                ("Priority Private", "private"), ("individuals", "unknown")]:
            self.assertEqual(parse_audience(label), expected)
        with self.assertRaises(ValueError): parse_tenor("about half a year")

    def test_combined_rates_and_wrong_money_are_rejected(self):
        for value in ["3.25% p.a. 4.25% p.a.", "3.25", "3.25% monthly"]:
            with self.subTest(value=value), self.assertRaises(ValueError): parse_rate(value)
        for value in ["US$12,000", "S$12,00", "about 12000"]:
            with self.subTest(value=value), self.assertRaises(ValueError): parse_money(value)
        self.assertEqual(parse_money("S$12,000"), "12000")
        self.assertEqual(parse_money("S$100,000 and above"), "100000")
        self.assertEqual(parse_money("SGD 200,000 or more"), "200000")

    def test_heading_rate_cannot_conflict_with_rate_field(self):
        self.raw["rows"][0]["audience_text"] = "Personal Banking: 9.25% p.a."
        with self.assertRaisesRegex(ValueError, "报价不一致"): bind_unit(self.raw, self.unit, "llm")

    def test_unknown_evidence_and_inexact_text_rejected(self):
        for reference in [{"page_id": "web-shot.png", "quote": "9 months", "locator": "web-shot.png table"},
                          {"page_id": "web-id", "quote": "9 months ... Personal Banking", "locator": "table"}]:
            with self.assertRaises(ValueError): check_reference(reference, {"web-id": self.web}, "llm")

    def test_references_are_bound_to_actual_text(self):
        bound=bind_unit(self.raw,self.unit,'llm')
        for reference in bound['rows'][0]['evidence']:self.assertEqual(reference['page_id'],'web-id')
        bad=deepcopy(self.unit);bad['text']='Unrelated source'
        with self.assertRaises(ValueError):bind_unit(self.raw,bad,'llm')

    def test_combine_preserves_conflicting_dates_and_tier_conditions(self):
        text = self.pdf["text"].split("[PDF page 2]")[0]
        pdf_unit = {"page": self.pdf, "text": text, "label": "pdf-text-page1"}
        terms = bind_terms_text(text, [], pdf_unit, "llm", [self.product])
        table = bind_unit(self.raw, self.unit, "llm")
        priority = deepcopy(table["rows"][0]); priority["audience_text"] = "Priority Private"
        table["rows"].append(priority)
        result, audit = combine([(self.unit, table), (pdf_unit, terms)], [self.web, self.pdf], self.config)
        personal, private = result["offers"]
        self.assertEqual((personal["tenor_value"], personal["tenor_unit"], personal["rate_pct"]), (9, "M", "3.25"))
        self.assertEqual(personal["channel"], "online_banking|sc_mobile")
        self.assertIsNone(personal["valid_from"])
        self.assertEqual(personal["valid_to"], "2027-01-31")
        self.assertNotIn("Priority Banking status", personal["conditions"])
        self.assertIn("Priority Banking status", private["conditions"])
        self.assertEqual(len(audit["source_periods"]), 2)
        self.assertTrue(any("来源日期冲突" in s for s in audit["review_required"]))

    def test_period_must_have_two_explicit_ordered_dates(self):
        self.assertEqual(parse_period("06 January 2027 to 31 January 2027"), ("2027-01-06", "2027-01-31"))
        for text in ["until 31 January 2027", "31 January 2027 to 06 January 2027"]:
            with self.assertRaises(ValueError): parse_period(text)

    def test_case_only_quote_binds_to_actual_source_without_accepting_paraphrase(self):
        self.web['text'] += ' simply transfer Fresh Funds to your account'
        self.unit['text'] = self.web['text']
        self.raw['terms'] = [dict(product_id=self.product['id'], kind='other', quote='Transfer Fresh Funds to your account', locator='paragraph')]
        result = bind_unit(self.raw,self.unit,'llm')
        self.assertEqual(result['terms'][0]['quote'],'transfer Fresh Funds to your account')
        self.assertEqual(len(result['source_spelling_repairs']),1)
        self.raw['terms'][0]['quote'] = 'Transfer new Funds to your account'
        with self.assertRaisesRegex(ValueError,'连续文字'):
            bind_unit(self.raw,self.unit,'llm')

    def test_contained_rate_period_requires_printed_website_hierarchy(self):
        self.web['text'] = self.web['text'].replace('31 January','20 January')
        self.unit['text'] = self.web['text']
        self.raw['periods'][0]['quote'] = self.raw['periods'][0]['quote'].replace('31 January','20 January')
        text = '1. Promotion Period from 01 January 2027 to 31 January 2027.\n2. Other terms.'
        pdf_unit = dict(page=self.pdf,text=text,label='pdf-text-page1')
        table = bind_unit(self.raw,self.unit,'llm')
        terms = bind_terms_text(text,[],pdf_unit,'llm',[self.product])
        result,audit = combine([(self.unit,table),(pdf_unit,terms)],[self.web,self.pdf],self.config)
        self.assertIsNone(result['offers'][0]['valid_to'])
        self.assertTrue(any('日期冲突' in message for message in audit['review_required']))
        text += '\n3. The Bank may refresh or change the Eligible tenor, Promotional Rate and Preferential Rate. Please refer to the website for the latest Eligible Tenor, Promotional Rate and Preferential Rate.'
        pdf_unit['text'] = text
        terms = bind_terms_text(text,[],pdf_unit,'llm',[self.product])
        result,audit = combine([(self.unit,table),(pdf_unit,terms)],[self.web,self.pdf],self.config)
        self.assertEqual((result['offers'][0]['valid_from'],result['offers'][0]['valid_to']),('2027-01-02','2027-01-20'))
        self.assertEqual(audit['date_resolutions'][0]['campaign_period'],['2027-01-01','2027-01-31'])
        self.assertFalse(any('日期冲突' in message for message in audit['review_required']))
        self.assertIn('条款总活动期',result['offers'][0]['conditions'])
        pdf_unit['text'] = text.replace('01 January','06 January')
        terms = bind_terms_text(pdf_unit['text'],[],pdf_unit,'llm',[self.product])
        result,audit = combine([(self.unit,table),(pdf_unit,terms)],[self.web,self.pdf],self.config)
        self.assertIsNone(result['offers'][0]['valid_from'])
        self.assertEqual(audit['date_resolutions'],[])

    def test_full_clauses_preserved_and_footer_excluded(self):
        text = "1. You must qualify; and\nb. keep the account.\n2. Second clause.\nPUBLIC\nFooter."
        unit = {"page": self.pdf, "image": "terms-p1.png", "label": "terms-p1.png"}
        result = bind_terms_text(text, [], unit, "llm", [self.product])
        self.assertIn("; and b.", result["clause_ledger"][0]["quote"])
        self.assertEqual(result["clause_ledger"][1]["quote"], "Second clause.")
        self.assertEqual(result["rows"], [])

    def test_missing_internal_clause_is_flagged(self):
        unit = {"page": self.pdf, "label": "terms-p1.png"}
        result = bind_terms_text("1. First.\n3. Third.", [], unit, "llm", [self.product])
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(result["unreadable"])

    def test_generic_schema_binds_document_ids_without_answer_values(self):
        schema = response_schema([self.web, self.pdf])
        self.assertEqual(schema["properties"]["offers"]["items"]["properties"]["evidence"]["items"]["properties"]["page_id"]["enum"], ["web-id", "pdf-id"])
        self.assertNotIn("1.70", TABLE_INSTRUCTIONS + PDF_INSTRUCTIONS)

    def test_text_tables_and_all_pdf_pages_are_processed_and_replay_bound(self):
        table={k:deepcopy(self.raw[k]) for k in ('rows','channels','periods','unreadable')};table['other_conditions']=[]
        response={'done':True,'done_reason':'stop','message':{'content':json.dumps(table)}}
        with tempfile.TemporaryDirectory() as temp:
            evidence=Path(temp)/'evidence';evidence.mkdir()
            with patch('market_rates.ollama_adapter.check_model'),patch('market_rates.ollama_adapter.request_json',return_value=response) as request:
                result,metadata=extract('llm',[self.web,self.pdf],evidence,self.config,'2027-01-12')
            self.assertEqual(request.call_count,1);self.assertEqual(len(metadata['units']),3)
            self.assertEqual(len(result['offers']),1)
            self.assertTrue(all('images' not in m for m in request.call_args.args[2]['messages']))
            with patch('market_rates.ollama_adapter.request_json',side_effect=AssertionError('No inference during replay')):
                replay,_=extract('llm',[self.web,self.pdf],evidence,self.config,'2027-01-12',replay_only=True)
                self.assertEqual(replay,result)
                self.web['text']+=' changed'
                with self.assertRaisesRegex(ValueError,'完全匹配'):extract('llm',[self.web,self.pdf],evidence,self.config,'2027-01-12',replay_only=True)


class ClauseReviewTests(unittest.TestCase):
    def metadata(self,left,right):
        return [dict(lane='llm',clause_ledger=[dict(page_id='p',number=1,quote=left,locator='1'),dict(page_id='p',number=2,quote=right,locator='2')])]

    def test_qualification_words_are_preserved(self):
        from market_rates.clause_review import normalized_text
        self.assertEqual(normalized_text("Bank’s rule; and\naccount."),normalized_text("Bank's rule; and account."))
        self.assertNotEqual(normalized_text('Rule; and account.'),normalized_text('Rule; account.'))
        self.assertEqual(clause_differences(self.metadata('One','Two')),[])

    def test_missing_or_duplicate_clause_requires_review(self):
        metadata=self.metadata('a','b');metadata[0]['clause_ledger'][1]['number']=3
        self.assertEqual(len(clause_differences(metadata)),1)
        metadata=self.metadata('a','b');metadata[0]['clause_ledger'].append(deepcopy(metadata[0]['clause_ledger'][0]))
        self.assertEqual(len(clause_differences(metadata)),1)

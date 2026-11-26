"""Regression tests for reusable report formatting, without Excel/dependencies."""
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from market_rates.report_xml import Book, M, NS, R, P, address, serialize, StyleMap
from market_rates.report_format import (Quote, customer_is_personal, winner, clean_amount,
    eligibility_from_amount, match_previous, format_report, serial, compact_notes, sgd_quotes,
    historical_rate, row_contexts, find_previous, format_matrix, format_history, first_delta, format_summary_details,
    public_quote_captions, quote_notes, show_audience_labels, format_premier, width_map, set_columns)
import json


def fixture(path,day,*,sources=True,prior_rate='0.018',missing_cache=False):
    """Deliberately different template/source row heights and native extensions."""
    wb=ET.Element(M+'workbook')
    sheets=ET.SubElement(wb,M+'sheets')
    rels=ET.Element(P+'Relationships')
    types=ET.Element('{http://schemas.openxmlformats.org/package/2006/content-types}Types')
    parts={}
    def sheet(name,values,merges=()):
        i=len(list(sheets))+1
        ET.SubElement(sheets,M+'sheet',name=name,sheetId=str(i),attrib={R+'id':f'rId{i}'})
        ET.SubElement(rels,P+'Relationship',Id=f'rId{i}',Type=R[1:-1]+'/worksheet',Target=f'worksheets/sheet{i}.xml')
        root=ET.Element(M+'worksheet')
        ET.SubElement(root,M+'dimension',ref='A1:L20')
        ET.SubElement(ET.SubElement(root,M+'sheetViews'),M+'sheetView',workbookViewId='0')
        ET.SubElement(root,M+'sheetFormatPr',defaultRowHeight='15')
        cols=ET.SubElement(root,M+'cols')
        ET.SubElement(cols,M+'col',min='1',max='12',width='15',customWidth='1')
        data=ET.SubElement(root,M+'sheetData')
        rows={}
        for loc,value in values.items():
            import re
            r=int(re.search(r'\d+',loc)[0])
            if r not in rows:
                rows[r]=ET.SubElement(data,M+'row',r=str(r),ht='60' if sources else '15',customHeight='1')
            c=ET.SubElement(rows[r],M+'c',r=loc,s='0')
            if isinstance(value,tuple):
                ET.SubElement(c,M+'f').text=value[0]
                if value[1] is not None:
                    ET.SubElement(c,M+'v').text=str(value[1])
            elif isinstance(value,(int,D)):
                ET.SubElement(c,M+'v').text=str(value)
            else:
                c.set('t','inlineStr')
                ET.SubElement(ET.SubElement(c,M+'is'),M+'t').text=str(value)
        if merges:
            node=ET.SubElement(root,M+'mergeCells',count=str(len(merges)))
            for merge in merges:
                ET.SubElement(node,M+'mergeCell',ref=merge)
        parts[f'xl/worksheets/sheet{i}.xml']=serialize(root)
    sgd={'A1':'新元促销利率市場调研','A2':'6 Months','A3':'日期','C3':serial(day),'D3':serial(date(2026,10,1)),
         'B4':'全部公开报价','H4':'当日最高报价变动','A5':'HSBC','B5':'old Personal',
         'C5':('明细_20261007!H4',None if missing_cache else '0.018'),'D5':D('0.017'),
         'B6':'old Premier','C6':D('0.020'),'D6':D('0.018'),
         'A7':'9 Months','A8':'日期','C8':serial(day),'D8':serial(date(2026,10,1)),
         'B9':'全部公开报价','H9':'当日最高报价变动','A10':'HLF','C10':D('0.019'),'D10':D('0.016'),
         'B11':'9M Personal','C11':D('0.016'),'D11':D('0.015')}
    sheet('最高报价汇总 ',{'B2':'新元促销利率','F2':serial(date(2026,10,1)),'B3':'Bank','C3':'1M','D3':'3M','E3':'6M','F3':'9M','G3':'12M','H3':'18M','I3':'24M','B4':'HSBC','E4':D('0.020'),'B5':'HLF','F5':D('0.019')})
    sheet('SGD促销',sgd,['A5:A6','C5:C6','A10:A11'])
    if sources:
        values={'A3':'银行','H3':'年利率'}
        rows=[('HSBC','6M','Personal',prior_rate,30000,None),('HSBC','6M','Premier','0.020',30000,None),
              ('HLF','8M','Personal','0.019',20000,None),('HLF','9M','Personal','0.016',100000,None)]
        for r,(bank,tenor,audience,rate,lo,hi) in enumerate(rows,4):
            for c,value in {1:bank,3:tenor,4:audience,5:'分行',6:lo,7:hi,8:D(rate),17:'须有账户'}.items():
                if value is not None:
                    values[address(r,c)]=value
        sheet('明细_'+day.strftime('%Y%m%d'),values)
    styles=ET.Element(M+'styleSheet')
    ET.SubElement(styles,M+'numFmts',count='0')
    fonts=ET.SubElement(styles,M+'fonts',count='1');font=ET.SubElement(fonts,M+'font');ET.SubElement(font,M+'sz',val='11');ET.SubElement(font,M+'name',val='Calibri')
    fills=ET.SubElement(styles,M+'fills',count='1');ET.SubElement(ET.SubElement(fills,M+'fill'),M+'patternFill',patternType='none')
    borders=ET.SubElement(styles,M+'borders',count='1');ET.SubElement(borders,M+'border')
    for name in ('cellStyleXfs','cellXfs'):
        node=ET.SubElement(styles,M+name,count='1');ET.SubElement(node,M+'xf',numFmtId='0',fontId='0',fillId='0',borderId='0',xfId='0')
    ET.SubElement(styles,M+'cellStyles',count='1');ET.SubElement(styles.find(M+'cellStyles'),M+'cellStyle',name='Normal',xfId='0',builtinId='0')
    parts.update({'xl/workbook.xml':serialize(wb),'xl/_rels/workbook.xml.rels':serialize(rels),
                  'xl/styles.xml':serialize(styles),'[Content_Types].xml':serialize(types),'custom/preserved.bin':b'keep exactly'})
    with zipfile.ZipFile(path,'w') as z:
        for name,value in parts.items():
            z.writestr(name,value)


class RuleTests(unittest.TestCase):
    def q(self,audience,rate,actual='9M'):
        return Quote('HLF','9M',audience,'柜台',D(20000),None,D(rate),'',actual,'source',4)

    def test_personal_only_with_no_premier_fallback(self):
        personal=self.q('Personal','0.016')
        self.assertIs(winner([personal,self.q('Premier','0.020')]),personal)
        self.assertIsNone(winner([self.q('Preferred','0.021')]))
        self.assertTrue(customer_is_personal('所有客群'))
        self.assertFalse(customer_is_personal('Premier Elite'))

    def test_quote_caption_retains_explicit_amount_boundaries(self):
        q=self.q('Personal','.019');q.maximum=D('199999.99');q.min_inclusive=False;q.max_inclusive=False
        self.assertEqual(q.amount,'S$>20,000–<199,999.99')

    def test_single_personal_class_omits_label_even_with_multiple_quotes(self):
        quotes=[self.q('Personal','.019'),self.q('personal','.018')]
        self.assertFalse(show_audience_labels(quotes))
        self.assertEqual(public_quote_captions(quotes),['1.90% counter S$≥20,000','1.80% counter S$≥20,000'])

    def test_all_and_personal_are_one_display_class(self):
        quotes=[self.q('Personal','.019'),self.q('所有客群','.019'),self.q('all','.018')]
        self.assertFalse(show_audience_labels(quotes))
        self.assertEqual(len(public_quote_captions(quotes)),2)
        self.assertTrue(all('Personal' not in c for c in public_quote_captions(quotes)))

    def test_same_caption_from_distinct_verified_records_is_not_dropped(self):
        first=self.q('Personal','.019');second=self.q('Personal','.019');second.row=5
        first.product=second.product='Same public product name'
        self.assertEqual(len(public_quote_captions([first,second])),2)

    def test_multiple_customer_classes_keep_every_label(self):
        quotes=[self.q('personal','.019'),self.q('premier','.020'),self.q('preferred','.021')]
        captions=public_quote_captions(quotes)
        self.assertTrue(show_audience_labels(quotes))
        for label in ('Personal','Premier','Preferred'):
            self.assertTrue(any(label in c for c in captions))
        self.assertEqual(winner(quotes).rate,D('.019'))

    def test_customer_labels_are_local_to_each_quote_group(self):
        personal=self.q('Personal','.019');premier=self.q('Premier','.020')
        self.assertIn('Personal',public_quote_captions([personal,premier])[0])
        self.assertNotIn('Personal',public_quote_captions([personal])[0])

    def test_single_personal_label_also_disappears_from_note_prefixes(self):
        first=self.q('Personal','.019');second=self.q('Personal','.018')
        first.notes='须新资金';second.notes='须有账户'
        notes=quote_notes([first,second])
        self.assertNotIn('Personal',notes)
        self.assertIn('1.90%：须新资金',notes)
        self.assertIn('1.80%：须有账户',notes)

    def test_mixed_customer_note_prefixes_remain_distinguishable(self):
        first=self.q('Personal','.019');second=self.q('Premier','.019')
        first.notes='须新资金';second.notes='须有账户'
        notes=quote_notes([first,second])
        self.assertIn('1.90% Personal：须新资金',notes)
        self.assertIn('1.90% Premier：须有账户',notes)

    def test_lone_restricted_customer_class_is_kept_as_eligibility_note(self):
        quotes=[self.q('Premier','.020')]
        self.assertNotIn('Premier',public_quote_captions(quotes)[0])
        self.assertIn('仅限 Premier 客户',quote_notes(quotes))
        self.assertIsNone(winner(quotes))

    def test_hlf_group_max_includes_actual_eight_months(self):
        result=winner([self.q('Personal','0.019','8M'),self.q('Personal','0.016')])
        self.assertEqual((result.actual,result.rate),('8M',D('0.019')))

    def test_amount_keeps_currency_and_exclusive_bounds(self):
        value='外币定存促销 / personal\nCNY ≥500 且<50,000；电子银行/柜台\n电子银行500起存；柜台20,000起存'
        self.assertEqual(clean_amount(value),'CNY ≥500 且<50,000')
        self.assertIn('柜台20,000起存',eligibility_from_amount(value))
        self.assertEqual(clean_amount('定期存款挂牌；SGD ≥1,000 且≤9,999；渠道原文未列明；personal'),'SGD ≥1,000 且≤9,999')

    def test_semicolon_upper_amount_is_preserved_for_all_currencies(self):
        for currency in ('SGD','USD','CNY','CNH','AUD','NZD','EUR','GBP','CAD','HKD'):
            with self.subTest(currency=currency):
                self.assertEqual(clean_amount(currency+' ≥10,000；≤1,000,000'),currency+' ≥10,000 且≤1,000,000')
                self.assertEqual(clean_amount(currency+' >10,000; '+currency+' <1,000,000'),currency+' >10,000 且<1,000,000')
                self.assertEqual(eligibility_from_amount(currency+' ≥10,000；≤1,000,000'),'')
        self.assertEqual(clean_amount('USD 等值 ≥5,000；<5 million'),'USD 等值 ≥5,000 且<5 million')

    def test_semicolon_qualification_caps_do_not_become_deposit_bounds(self):
        for qualification in ('个人合计 SGD <1,000,000','AUM ≥200,000','TRB ≥200,000','每名客户最高合计 USD 1,000,000'):
            with self.subTest(qualification=qualification):
                value='USD ≥5,000；'+qualification
                self.assertEqual(clean_amount(value),'USD ≥5,000')
                self.assertIn(qualification,eligibility_from_amount(value))
        self.assertEqual(clean_amount('SGD ≥10,000；USD ≤1,000,000'),'SGD ≥10,000')

    def test_missing_previous_is_not_zero_or_other_customer(self):
        self.assertEqual(match_previous('USD ≥5,000','HSBC',[('Premier\nUSD ≥5,000',D('.02'))]),'-')
        self.assertEqual(match_previous('USD ≥5,000','HSBC',[('personal\nUSD ≥5,000',D('.01'))]),D('.01'))

    def test_same_amount_different_qualification_is_not_previous_product(self):
        current='换汇促销 / personal\nUSD ≥50,000\n必须换汇'
        other='新资金促销 / personal\nUSD ≥50,000\n外部新资金'
        self.assertEqual(match_previous(current,'CITI',[(other,D('.035'))],currency='USD'),'-')

    def test_split_band_uses_only_a_complete_unambiguous_parent(self):
        self.assertEqual(match_previous('USD ≥5,000 且<100,000','ICBC',[('USD ≥5,000',D('.0365'))]),D('.0365'))
        self.assertEqual(match_previous('USD ≥100,000','ICBC',[('USD ≥5,000',D('.0365'))]),D('.0365'))
        self.assertEqual(match_previous('USD ≥5,000','ICBC',[('USD ≥5,000 且<100,000',D('.0365'))]),'-')
        self.assertEqual(match_previous('personal\nUSD ≥5,000','A',[('USD ≥5,000',D('.01')),('USD ≥5,000',D('.02'))]),'-')

    def test_exclusive_boundary_is_not_the_same_as_inclusive(self):
        self.assertEqual(match_previous('USD ≤5,000','A',[('USD <5,000',D('.01'))]),'-')
        self.assertEqual(match_previous('USD ≥10,000','BEA',[('≥10,000',D('.01'))],currency='CNY'),'-')

    def test_historical_text_rates(self):
        self.assertEqual(historical_rate('2.35%电子/2.35%柜台'),D('.0235'))
        self.assertEqual(historical_rate('1.5% personal / 1.8% premier'),D('.015'))
        self.assertEqual(historical_rate('premier 2.00%'),'-')

    def test_qualification_not_process_narration(self):
        value='须换汇；须有账户；公告已截止；本周已检查，数字未调整（待复核）；证据 2026-10-07'
        self.assertEqual(compact_notes(value),'须换汇；须有账户；公告已截止')

    def test_unused_ignorable_namespace_survives(self):
        raw=(f'<worksheet xmlns="{NS}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
             'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" mc:Ignorable="x14"><sheetData/></worksheet>').encode()
        output=serialize(ET.fromstring(raw),raw)
        self.assertIn(b'xmlns:x14=',output)
        ET.fromstring(output)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.current=self.root/'current.xlsx';self.prior=self.root/'prior.xlsx';self.template=self.root/'template.xlsx'
        fixture(self.current,date(2026,10,7))
        fixture(self.prior,date(2026,10,6),prior_rate='0.017')
        fixture(self.template,date(2026,10,1),sources=False)

    def tearDown(self):
        self.tmp.cleanup()

    @patch('market_rates.report_format.workbook_policy',return_value={'complete_public_quotes':False})
    def test_complete_pipeline_original_columns_and_native_zero_rule(self,_policy):
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.current,self.prior,self.template)}
        output=self.root/'out.xlsx'
        with patch('market_rates.report_format.find_previous',side_effect=AssertionError('External history must not be used')):
            report=format_report(self.current,self.template,output)
        b=Book(output);s=b.sheets['SGD促销']
        self.assertEqual(list(b.sheets),['最高报价汇总 ','SGD促销'])
        self.assertEqual(b.sheets['最高报价汇总 '].value(4,5),D('.018'))
        self.assertEqual(b.sheets['最高报价汇总 '].value(5,6),D('.016'))
        self.assertEqual(s.value(3,4),serial(date(2026,10,1)))
        self.assertIsNone(s.value(3,5))
        self.assertIsNone(b.sheets['最高报价汇总 '].value(2,7))
        self.assertEqual(first_delta(s),8)
        self.assertEqual(s.value(5,3),D('.018'))
        self.assertEqual(s.value(5,4),D('.018'))
        self.assertEqual(s.value(5,8),D('0'))
        self.assertEqual(s.value(9,8),D('0'))
        self.assertTrue(any(q['tenor']=='8M' and q['rate']=='0.019' for q in report['sgd_winners']))
        self.assertEqual(report['comparison_mode'],'latest_two_columns')
        self.assertIsNone(report['previous'])
        icons=s.root.find(M+'conditionalFormatting/'+M+'cfRule/'+M+'iconSet')
        self.assertEqual([(x.get('type'),x.get('val'),x.get('gte')) for x in icons],
                         [('num','0',None),('num','0','1'),('num','0','0')])
        self.assertEqual(b.parts['custom/preserved.bin'],b'keep exactly')
        self.assertTrue(report['sources_unchanged'])
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in hashes.items()))

    def test_complete_public_quotes_share_one_personal_row_and_archive_overflow(self):
        b=Book(self.current);detail=b.sheets['明细_20261007']
        for row in range(8,38):
            for column,value in {1:'HSBC',2:'独立产品'+str(row),3:'6M',4:'Personal',5:'在线',6:D(10000),8:D('.017'),17:'须新资金；每个产品的资格条件均须保留'}.items():detail.put(row,column,value)
        b.save(self.current)
        out=self.root/'all-products.xlsx';audit=format_report(self.current,self.template,out);sheet=Book(out).sheets['SGD促销']
        self.assertEqual(sheet.value(5,4),D('.017'))
        self.assertEqual(sheet.value(5,3),D('.018'))
        self.assertEqual(sheet.value(5,first_delta(sheet)),D('.001'))
        self.assertEqual(list(row_contexts(sheet,1)[0].values()).count(('SGD','6M','HSBC')),1)
        self.assertGreater(audit['rows_removed']['SGD促销'],0)
        self.assertTrue(any(x['selected']=='.017' or x['selected']=='0.017' for x in audit['historical_consolidations']))
        overflow=Book(out).sheets['业务说明']
        self.assertTrue(any('独立产品37' in str(overflow.value(r,2)) for r in overflow.rows))
        self.assertEqual(len(audit['full_sgd_public_rows']),34)
        self.assertTrue(all(float(sheet.rows[item['row']].get('ht'))<=409 for item in audit['full_sgd_public_rows']))
        self.assertTrue(any('独立产品37' in item['caption'] for item in audit['full_sgd_public_rows']))

    def test_no_overwrite_and_no_partial_output_when_cache_missing(self):
        output=self.root/'existing.xlsx';output.write_bytes(b'existing')
        with self.assertRaises(ValueError):
            format_report(self.current,self.template,output)
        self.assertEqual(output.read_bytes(),b'existing')
        fixture(self.current,date(2026,10,7),missing_cache=True)
        new=self.root/'new.xlsx'
        with self.assertRaisesRegex(ValueError,'Missing formula cache'):
            format_report(self.current,self.template,new)
        self.assertFalse(new.exists())

    def test_dry_run_does_not_create_workbook(self):
        output=self.root/'out.xlsx'
        report=format_report(self.current,self.template,output,dry_run=True)
        self.assertFalse(output.exists())
        self.assertEqual(report['previous_date'],'2026-10-01')
        self.assertEqual(report['verification']['in_memory_checks'],'passed')

    def test_adjacent_missing_rate_does_not_borrow_from_older_column(self):
        b=Book(self.current);s=b.sheets['SGD促销']
        s.put(3,5,serial(date(2026,9,29)))
        for r in (5,6):
            s.put(r,4,None);s.put(r,5,D('.025'))
        b.save(self.current)
        out=self.root/'missing.xlsx'
        report=format_report(self.current,self.template,out)
        s=Book(out).sheets['SGD促销']
        self.assertIsNone(s.value(5,4))
        self.assertEqual(s.value(5,5),D('.025'))
        self.assertEqual(s.value(5,8),'-')
        self.assertTrue(any(x['bank']=='HSBC' for x in report['missing_previous']))

    def test_each_section_keeps_its_own_adjacent_date_including_same_day(self):
        b=Book(self.current);b.sheets['SGD促销'].put(8,4,serial(date(2026,10,7)));b.save(self.current)
        out=self.root/'section_dates.xlsx'
        report=format_report(self.current,self.template,out)
        s=Book(out).sheets['SGD促销']
        headers=row_contexts(s,1)[1]
        self.assertEqual([s.value(r,4) for r in headers],[serial(date(2026,10,1)),serial(date(2026,10,7)),None])
        self.assertIsNone(report['previous_date'])

    def test_missing_adjacent_date_fails_without_inventing_a_column(self):
        b=Book(self.current);b.sheets['SGD促销'].put(3,4,None);b.save(self.current)
        out=self.root/'invalid_date.xlsx'
        with self.assertRaisesRegex(ValueError,'invalid latest two date columns'):
            format_report(self.current,self.template,out)
        self.assertFalse(out.exists())

    def test_external_previous_argument_is_explicitly_retired(self):
        with self.assertRaisesRegex(ValueError,'previous_path 已取消'):
            format_report(self.current,self.template,dry_run=True,previous_path=self.prior)

    def test_new_section_without_historical_date_or_rates_stays_blank(self):
        b=Book(self.current);s=b.sheets['SGD促销']
        for r in (3,5,6):s.put(r,4,None)
        b.save(self.current)
        out=self.root/'new_section.xlsx'
        report=format_report(self.current,self.template,out)
        s=Book(out).sheets['SGD促销']
        self.assertIsNone(s.value(3,4))
        self.assertIsNone(s.value(5,4))
        self.assertEqual(s.value(5,8),'-')
        self.assertIsNone(report['comparison_columns']['SGD促销'][0]['previous_date'])

    def test_new_product_for_existing_bank_reselects_personal_maximum(self):
        b=Book(self.current);detail=b.sheets['明细_20261007']
        for c,v in {1:'HSBC',3:'6M',4:'Personal',5:'在线',6:D(10000),8:D('.025'),17:'新产品'}.items():
            detail.put(8,c,v)
        s=b.sheets['SGD促销']
        s.remap(rowmap={r:r if r<7 else r+1 for r in range(1,s.maxrow+1)})
        s.put(7,2,'new product');s.put(7,3,D('.025'),formula='明细_20261007!H8')
        b.save(self.current)
        out=self.root/'new_product.xlsx'
        a=format_report(self.current,self.template,out)
        chosen=next(q for q in a['sgd_winners'] if q['bank']=='HSBC' and q['tenor']=='6M')
        self.assertEqual(D(chosen['rate']),D('.025'))
        result=Book(out)
        self.assertEqual(result.sheets['最高报价汇总 '].value(4,5),D('.025'))

    def test_deleted_personal_winner_does_not_fall_back_to_premier(self):
        b=Book(self.current);b.sheets['明细_20261007'].put(4,8,None)
        b.sheets['SGD促销'].put(5,3,'-');b.save(self.current)
        out=self.root/'withdrawn.xlsx'
        a=format_report(self.current,self.template,out)
        self.assertFalse(any(q['bank']=='HSBC' for q in a['sgd_winners']))
        self.assertEqual(Book(out).sheets['SGD促销'].value(5,3),'-')

    def test_new_bank_in_full_input_is_not_limited_to_template_bank_rows(self):
        b=Book(self.current);detail=b.sheets['明细_20261007']
        for c,v in {1:'NEWBANK',3:'6M',4:'Personal',5:'在线',6:D(10000),8:D('.025')}.items():
            detail.put(8,c,v)
        s=b.sheets['SGD促销']
        s.remap(rowmap={r:r if r<7 else r+1 for r in range(1,s.maxrow+1)})
        s.put(7,1,'NEWBANK');s.put(7,2,'new');s.put(7,3,D('.025'),formula='明细_20261007!H8')
        summary=b.sheets['最高报价汇总 '];summary.put(6,2,'NEWBANK');summary.put(6,5,D('.025'))
        b.save(self.current)
        out=self.root/'new_bank.xlsx'
        a=format_report(self.current,self.template,out)
        self.assertTrue(any(q['bank']=='NEWBANK' and D(q['rate'])==D('.025') for q in a['sgd_winners']))
        self.assertEqual(Book(out).sheets['最高报价汇总 '].value(6,5),D('.025'))

    def test_expired_but_present_product_is_flagged_not_automatically_deleted(self):
        b=Book(self.current);b.sheets['明细_20261007'].put(4,11,serial(date(2026,10,5)));b.save(self.current)
        out=self.root/'expired_reference.xlsx'
        format_report(self.current,self.template,out)
        s=Book(out).sheets['SGD促销']
        self.assertEqual(s.value(5,3),D('.018'))
        self.assertTrue(any('公告已截止' in str(s.value(r,c)) for r,c in s.cells))

    def test_unmapped_new_tenor_fails_before_output(self):
        b=Book(self.current);b.sheets['SGD促销'].put(2,1,'2 Months')
        b.sheets['明细_20261007'].put(4,3,'2M');b.save(self.current)
        out=self.root/'unmapped.xlsx'
        result=format_report(self.current,self.template,out)
        self.assertTrue(out.exists())
        self.assertTrue(any(q['tenor']=='2M' for q in result['sgd_winners']))

    def test_legacy_monthr_header_does_not_mix_tenors(self):
        book=Book(self.current);sheet=book.sheets['SGD促销']
        sheet.put(7,1,'Tenor: 3 monthR')
        self.assertEqual(row_contexts(sheet,1)[0][10][1],'3M')
        sheet.put(7,1,'Tenor: unsupported')
        with self.assertRaisesRegex(ValueError,'unrecognized tenor'):
            row_contexts(sheet,1)

    def test_previous_selection_excludes_partial_failed_and_replay(self):
        for index,mode,status,dates in [(1,'live','complete',{'sgd':'2026-10-06','board':'2026-10-06','fx':'2026-10-06'}),
                                       (2,'live','failed',{'sgd':'2026-10-06','board':'2026-10-06','fx':'2026-10-06'}),
                                       (3,'correction','complete',{'sgd':'2026-10-06','board':'2026-10-06','fx':'2026-10-06'}),
                                       (4,'live','complete',{'sgd':'2026-10-06'})]:
            folder=self.root/'runs'/f'all-{index}';folder.mkdir(parents=True)
            data={'mode':mode,'status':status,'source_dates':dates,'started_at':f'2026-10-06T1{index}:00:00','report':str(self.prior)}
            (folder/'workflow.json').write_text(json.dumps(data),encoding='utf8')
        self.assertEqual(find_previous(self.root,self.current,date(2026,10,7)),self.prior)

    def blank_sheet(self, book, name, values, merges=()):
        s=book.sheets['SGD促销']
        s.name=name;s.cells={};s.rows={}
        for tag in ('mergeCells','conditionalFormatting'):
            for old in s.root.findall(M+tag):s.root.remove(old)
        for (r,c),value in values.items():s.put(r,c,value,'0')
        if merges:
            node=ET.Element(M+'mergeCells',count=str(len(merges)))
            for ref in merges:ET.SubElement(node,M+'mergeCell',ref=ref)
            s.insert_element(node)
        return s

    def test_matrix_uses_named_notes_column_not_styled_used_range(self):
        b,ref=Book(self.current),Book(self.template)
        values={(1,2):'澳元挂牌利率',(2,2):'Bank',(2,3):'Amount',(2,4):'1M',
                (2,14):'其他要求',(3,2):'DBS/POSB',(3,3):'AUD ≥25,000；须有账户',
                (3,4):D('.0316'),(3,14):'另须达到SGD5,000等值；采集 2026-10-07',(2,21):None}
        s=self.blank_sheet(b,'AUD挂牌',values)
        t=self.blank_sheet(ref,'AUD挂牌',values)
        format_matrix(s,t,StyleMap(b,ref),{})
        self.assertEqual(s.value(2,14),'备注')
        self.assertIn('须有账户',s.value(3,14))
        self.assertIn('另须达到SGD5,000等值',s.value(3,14))
        self.assertNotIn('采集',s.value(3,14))
        self.assertEqual(s.value(3,3),'AUD ≥25,000')
        self.assertEqual(s.value(3,4),D('.0316'))
        self.assertEqual(s.maxcol,14)

    def test_amount_tier_deltas_are_independently_visible(self):
        b,ref=Book(self.current),Book(self.template)
        values={(1,2):'美元促销利率',(2,2):'Tenor: 1 month',(3,2):'银行',(3,3):'金额',
                (3,4):serial(date(2026,10,7)),(3,5):serial(date(2026,10,1)),(3,8):'当日增幅',
                (4,2):'ICBC',(4,3):'USD ≥5,000 且<20,000',(4,4):D('.035'),(4,5):D('.035'),
                (5,3):'USD ≥20,000',(5,4):D('.037'),(5,5):D('.036')}
        s=self.blank_sheet(b,'USD促销',values,('B4:B5','H4:H5'))
        t=self.blank_sheet(ref,'USD促销',values)
        audit={'rows_removed':{},'missing_previous':[],'retained_amount_conflicts':[]}
        format_history(s,t,StyleMap(b,ref),audit)
        delta=first_delta(s)
        self.assertEqual(s.value(4,delta),D('0'))
        self.assertEqual(s.value(5,delta),D('.001'))
        merges=[n.get('ref') for n in s.root.findall(M+'mergeCells/'+M+'mergeCell')]
        self.assertNotIn('H4:H5',merges)
        self.assertEqual(s.value(3,5),serial(date(2026,10,1)))
        self.assertIn('B4:B5',merges)

    def test_premier_new_tenor_does_not_get_overwritten_by_notes(self):
        b,ref=Book(self.current),Book(self.template)
        current={(1,1):'新元 2026-10-08',(2,1):'Bank',(2,2):'准入门槛',(2,3):'Minimum Amount',
                 (2,4):'3M',(2,5):'24M',(2,6):'其他',(3,1):'RHB',(3,2):'Premier',
                 (3,3):'SGD ≥20,000',(3,4):D('.02'),(3,5):D('.023'),(3,6):'须新资金'}
        reference={(1,1):'新元',(2,1):'Bank',(2,2):'准入门槛',(2,3):'Minimum Amount',
                   (2,4):'3M',(2,5):'其他',(3,1):'RHB',(3,3):'SGD ≥20,000',(3,4):D('.019'),(3,5):'旧备注'}
        s=self.blank_sheet(b,'Premier Rates',current)
        r=self.blank_sheet(ref,'Premier Rates',reference)
        set_columns(r,{5:{'hidden':'1','width':'0'}})
        format_premier(s,r,StyleMap(b,ref))
        self.assertEqual(s.value(3,5),D('.023'))
        self.assertEqual(s.value(3,6),'须新资金')
        self.assertEqual(width_map(s)[5]['hidden'],'0')
        self.assertGreaterEqual(float(width_map(s)[5]['width']),4)

    def test_premier_live_hidden_one_month_is_visible_across_currency_sections(self):
        b,ref=Book(self.current),Book(self.template)
        values={(1,1):'新元 2026-10-08',(2,1):'Bank',(2,2):'准入门槛',(2,3):'Minimum Amount',
                (2,4):'1M',(2,5):'24M',(2,6):'其他',(3,1):'RHB',(3,2):'Premier',
                (3,3):'SGD ≥20,000',(3,4):D('.012'),(3,5):'-',
                (5,1):'美元 2026-10-08',(6,1):'Bank',(6,2):'准入门槛',(6,3):'Minimum Amount',
                (6,4):'1M',(6,5):'24M',(6,6):'其他',(7,1):'HSBC',(7,2):'Premier',
                (7,3):'USD ≥30,000',(7,4):'-',(7,5):'-'}
        s=self.blank_sheet(b,'Premier Rates',values)
        r=self.blank_sheet(ref,'Premier Rates',{key:value for key,value in values.items() if key[0]<=3})
        set_columns(r,{4:{'hidden':'1','width':'0'},5:{'hidden':'1','width':'12'}})
        format_premier(s,r,StyleMap(b,ref))
        self.assertEqual(s.value(3,4),D('.012'))
        self.assertEqual(width_map(s)[4]['hidden'],'0')
        self.assertGreaterEqual(float(width_map(s)[4]['width']),4)
        self.assertEqual(width_map(s)[5]['hidden'],'1')
        s.put(3,4,'-')
        format_premier(s,r,StyleMap(b,ref))
        self.assertEqual(width_map(s)[4]['hidden'],'1')

    def test_same_rate_and_amount_distinct_products_are_not_collapsed(self):
        b,ref=Book(self.current),Book(self.template)
        values={(2,2):'Tenor: 1 month',(3,2):'银行',(3,3):'金额',
                (3,4):serial(date(2026,10,7)),(3,5):serial(date(2026,10,1)),(3,8):'当日增幅',
                (4,2):'CITI',(4,3):'换汇促销 / personal\nUSD ≥50,000\n须换汇',(4,4):D('.035'),
                (5,3):'新资金促销 / personal\nUSD ≥50,000\n须外部新资金',(5,4):D('.035')}
        s=self.blank_sheet(b,'USD促销',values,('B4:B5',))
        r=self.blank_sheet(ref,'USD促销',values)
        audit={'rows_removed':{},'missing_previous':[],'retained_amount_conflicts':[]}
        format_history(s,r,StyleMap(b,ref),audit)
        self.assertEqual(audit['rows_removed']['USD促销'],0)
        self.assertEqual(s.value(4,4),D('.035'))
        self.assertEqual(s.value(5,4),D('.035'))

    def test_summary_uses_winning_amount_and_preserves_unlisted_tenor_cache(self):
        b,ref=Book(self.current),Book(self.template)
        source=self.blank_sheet(b,'USD促销',{(2,2):'Tenor: 1 month',(3,2):'银行',
            (3,4):serial(date(2026,10,7)),(3,8):'当日增幅',
            (4,2):'ICBC',(4,3):'USD ≥5,000',(4,4):D('.036')})
        b.sheets['USD促销']=source
        summary=b.sheets['最高报价汇总 ']
        for (r,c),v in {(27,2):'美元促销利率',(28,2):'Bank',(28,3):'1M',(28,4):'18M',
                        (28,8):'Minimum Amount',(28,9):'OTHERS',(29,2):'ICBC',
                        (29,3):D('.036'),(29,4):D('.03'),(29,8):'各产品门槛见USD促销',
                        (29,9):'须新资金'}.items():summary.put(r,c,v,'0')
        audit={}
        format_summary_details(b,ref.sheets['最高报价汇总 '],StyleMap(b,ref),audit)
        self.assertEqual(summary.value(29,8),'USD ≥5,000')
        self.assertEqual(summary.value(29,4),D('.03'))
        self.assertEqual(summary.value(29,9),'须新资金')

    def test_summary_cannot_replace_personal_with_higher_exclusive_rate(self):
        b,ref=Book(self.current),Book(self.template)
        source=self.blank_sheet(b,'USD促销',{(2,2):'Tenor: 1 month',(3,2):'银行',
            (3,4):serial(date(2026,10,7)),(4,2):'ICBC',(4,3):'USD ≥5,000',(4,4):D('.036'),
            (5,3):'USD ≥100,000',(5,4):D('.05')},merges=['B4:B5'])
        b.sheets['USD促销']=source;summary=b.sheets['最高报价汇总 ']
        for (r,c),v in {(27,2):'美元促销利率',(28,2):'Bank',(28,3):'1M',
                        (28,8):'Minimum Amount',(28,9):'OTHERS',(29,2):'ICBC',
                        (29,3):D('.036'),(29,8):'USD ≥5,000'}.items():summary.put(r,c,v,'0')
        audit={'retail_history_rows':{'USD促销':{4:True,5:False}}}
        format_summary_details(b,ref.sheets['最高报价汇总 '],StyleMap(b,ref),audit)
        self.assertEqual(summary.value(29,3),D('.036'))
        self.assertEqual(summary.value(29,8),'USD ≥5,000')

    def test_actual_board_tenor_short_labels_have_separate_contexts(self):
        b=Book(self.current)
        source=self.blank_sheet(b,'USD挂牌',{(2,2):'2M',(3,2):'银行',(4,2):'BOC',
            (6,2):'14D',(7,2):'银行',(8,2):'BOC',(10,2):'8M',(11,2):'银行',(12,2):'BOC'})
        contexts,_,_=row_contexts(source,2)
        self.assertEqual([contexts[r][1] for r in (4,8,12)],['2M','14D','8M'])

    def test_currency_title_cannot_replace_adjacent_actual_tenor(self):
        from market_rates.report_format import finish_history_headers
        book,template=Book(self.current),Book(self.template)
        values={(2,1):'加元促销利率',(2,2):'Tenor: 7 days',(3,2):'银行',
                (3,4):serial(date(2026,10,7)),(3,8):'当日增幅',(4,2):'BEA',(4,3):'CAD ≥100,000',
                (5,1):'瑞士法郎促销利率',(5,2):'Tenor: 2 months',(6,2):'银行',
                (6,4):serial(date(2026,10,7)),(6,8):'当日增幅',(7,2):'BEA',(7,3):'CHF ≥100,000'}
        sheet=self.blank_sheet(book,'其他外币促销利率',values)
        ref=self.blank_sheet(template,'其他外币促销利率',values)
        before=row_contexts(sheet,2)[0]
        finish_history_headers(sheet,ref,StyleMap(book,template),2,4,8)
        self.assertEqual(row_contexts(sheet,2)[0],before)
        self.assertEqual(before[4],('CAD','7D','BEA'));self.assertEqual(before[7],('CHF','2M','BEA'))


if __name__=='__main__':
    unittest.main()

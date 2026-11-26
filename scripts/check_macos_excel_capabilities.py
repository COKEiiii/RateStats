"""Native cross-day history regression on disposable, synthetic workbooks."""
import argparse
from copy import copy
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.formatting.rule import IconSetRule
from openpyxl.styles import Border,Font,PatternFill,Side,Alignment

from market_rates.common import save
from market_rates.table_validation import sha
from market_rates.report_xml import Book,M,pos
from scripts.excel_native import (excel_app,copied_book,open_book,insert_columns,insert_rows,
                                 copy_formats,set_formula,remove_quote_icons,export_preview)


def check(out):
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    source=out/'source.xlsx';target=out/'result.xlsx'
    book=openpyxl.Workbook();sheet=book.active;sheet.title='历史测试'
    for row in [['银行','条件',datetime(2026,10,8),datetime(2026,10,7),'当日增幅'],
                ['测试银行','10,000起',0.02,0.018,'=C2-D2'],
                [None,'20,000起',0.03,0.024,'=C3-D3']]:sheet.append(row)
    sheet.merge_cells('A2:A3')
    sheet['A2'].alignment=Alignment(vertical='center')
    for column,width in [('A',20),('B',18),('C',18),('D',18),('E',16)]:
        sheet.column_dimensions[column].width=width
    for address in ('C1','D1'):sheet[address].number_format='yyyy-mm-dd'
    edge=Side(style='thin',color='FF112233')
    for row in sheet.iter_rows(min_row=2,max_row=3,min_col=3,max_col=5):
        for cell in row:
            cell.font=Font(name='Songti SC',size=11,bold=True,color='FF000000')
            cell.fill=PatternFill('solid',fgColor='FFE8F4FD')
            cell.border=Border(left=edge,right=edge,top=edge,bottom=edge)
            cell.number_format='0.00%'
    sheet.conditional_formatting.add('C2:D3',IconSetRule('3Arrows','num',[0,0.02,0.03]))
    sheet.conditional_formatting.add('E2:E3',IconSetRule('3Arrows','num',[-0.001,0,0.001]))
    linked=book.create_sheet('联动')
    linked['A1']="='历史测试'!C2";linked['B1']="=SUM('历史测试'!C2:C3)"
    book.save(source);before=sha(source)
    with excel_app() as app:
        native=copied_book(app,source,target)
        try:
            sheet=native.sheets['历史测试']
            insert_columns(sheet,3)
            copy_formats(sheet.range('D1:D3'),sheet.range('C1:C3'))
            sheet.range('C1').value=datetime(2026,10,9)
            sheet.range('C2:C3').value=[[0.025],[0.035]]
            insert_rows(sheet,3,1,last_col=6)
            copy_formats(sheet.range('B2:F2'),sheet.range('B3:F3'))
            sheet.range('B3:F3').clear_contents()
            sheet.range('B3').value='新增条件';sheet.range('C3').value=0
            for row in (2,3,4):
                set_formula(sheet.range(f'F{row}'),f'=IF(COUNT(C{row}:D{row})=2,ROUND(C{row}-D{row},8),"-")')
            app.calculate()
            assert native.sheets['联动'].range('A1').value==0.02
            assert abs(native.sheets['联动'].range('B1').value-0.05)<1e-10
            assert 'D2' in native.sheets['联动'].range('A1').formula
            assert 'D2:D4' in native.sheets['联动'].range('B1').formula
            assert sheet.range('A2').merge_area.address=='$A$2:$A$4'
            sheet.range('C2').value=0.02;sheet.range('C4').value=0.025;app.calculate()
            assert sheet.range('F2').value==0
            assert abs(sheet.range('F4').value+0.005)<1e-10
            sheet.range('C2').value=0.025;sheet.range('C4').value=0.035;app.calculate()
            remove_quote_icons(native,'历史测试',3)
            native=open_book(app,target,readonly=True)
            sheet=native.sheets['历史测试']
            assert sheet.range('C3').value==0 and sheet.range('D3').value is None
            assert sheet.range('F3').value=='-'
            assert abs(sheet.range('F2').value-0.005)<1e-10
            assert abs(sheet.range('F4').value-0.005)<1e-10
            export_preview(sheet,'A1:F4',out/'preview.pdf',save_source=False)
        finally:native.close()
    old=openpyxl.load_workbook(source);new=openpyxl.load_workbook(target)
    try:
        for a,b in [('C2','D2'),('C3','D4'),('D2','E2'),('D3','E4')]:
            left,right=old['历史测试'][a],new['历史测试'][b]
            assert left.value==right.value
            assert all(getattr(left.border,side)==getattr(right.border,side)
                       for side in ('left','right','top','bottom'))
            assert copy(left.fill)==copy(right.fill)
            assert (left.font.name,left.font.sz,left.font.bold)==(right.font.name,right.font.sz,right.font.bold)
    finally:old.close();new.close()
    xml=Book(target).sheets['历史测试'].root
    icons=[cf.get('sqref') for cf in xml.findall(M+'conditionalFormatting')
           if any(rule.get('type')=='iconSet' for rule in cf)]
    def columns(refs):
        for ref in refs.split():
            span=ref.split(':');first=pos(span[0])[1];last=pos(span[-1])[1]
            yield from range(first,last+1)
    assert icons and all(c!=3 for ref in icons for c in columns(ref))
    assert any(c==6 for ref in icons for c in columns(ref))
    assert sha(source)==before
    result=dict(passed=True,synthetic=True,insert_row=True,insert_date_column=True,reference_adjustment=True,
        saved_cache=True,historical_values_and_styles=True,zero_and_missing=True,positive_negative_zero_deltas=True,
        quote_icons_removed=True,delta_icons_retained=True,pdf=True,source_unchanged=True,
        source=str(source),source_sha256=before,output=str(target),sha256=sha(target))
    save(out/'checks.json',result);print('Native cross-day Excel capability regression passed',flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True)
    check(parser.parse_args().out)

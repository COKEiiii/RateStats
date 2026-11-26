"""Editable current-run Premier inputs and stable-key Excel references."""
from decimal import Decimal
import hashlib,json,re
import xml.etree.ElementTree as ET
from .report_xml import M,numeric,address

DETAIL_SHEET='本期专属明细'
IDENTITY=('bank','currency','product_id','audience','channel','tenor','amount_currency',
          'amount_is_equivalent','amount_min','amount_max','min_inclusive','max_inclusive',
          'fresh_funds','valid_from','valid_to','conditions')
HEADERS=['记录编号','银行','币种','产品','客群','渠道','期限','起存金额','金额上限','年利率（可修改）','金额与资格条件','采集日期','来源网址']

def record_key(offer):
    raw=json.dumps({k:offer.get(k) for k in IDENTITY},ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def reference_formula(key,last):
    keys=f"'{DETAIL_SHEET}'!$A$4:$A${last}"
    values=f"'{DETAIL_SHEET}'!$J$4:$J${last}"
    found=f'INDEX({values},MATCH("{key}",{keys},0))'
    # COUNT(INDEX(...)) observes a blank as a missing cell and keeps true zero.
    return f'IF(COUNTIFS({keys},"{key}")<>1,"待核实",IF(COUNT({found})=1,{found},"-"))'

def build_details(book,offers,style):
    sheet=book.sheets.get(DETAIL_SHEET) or book.add_sheet(DETAIL_SHEET)
    sheet.rows={};sheet.cells={}
    for child in list(sheet.root):
        if child.tag!=M+'sheetData':sheet.root.remove(child)
    sheet.node.set('state','visible')
    sheet.put(1,1,'本期专属客群报价',style['title'])
    sheet.put(2,1,'修改J列利率后，Premier Rates自动更新；空白显示“－”，0%保留。新增产品或修改资格后请重新生成。',style['text'])
    sheet.height(1,26);sheet.height(2,30);sheet.height(3,30)
    merges=ET.Element(M+'mergeCells',count='2')
    for row in (1,2):ET.SubElement(merges,M+'mergeCell',ref=f'A{row}:M{row}')
    sheet.insert_element(merges)
    for c,title in enumerate(HEADERS,1):sheet.put(3,c,title,style['header'])
    unique={}
    for offer in offers:
        key=record_key(offer)
        if key in unique and Decimal(unique[key]['rate'])!=Decimal(offer['rate']):
            raise ValueError('Premier 明细同一记录有冲突报价')
        unique.setdefault(key,offer)
    for row,(key,item) in enumerate(unique.items(),4):
        from .premier_sync import amount_text
        values=[key,item['bank'],item['display_currency'],item['product_name'],item['audience'],item['channel'],item['tenor'],
                Decimal(item['amount_min']) if item.get('amount_min') else None,
                Decimal(item['amount_max']) if item.get('amount_max') else None,
                Decimal(item['rate']),amount_text(item)+'；'+str(item.get('conditions') or ''),item['collected_on'],'\n'.join(item['urls'])]
        for c,value in enumerate(values,1):sheet.put(row,c,value,style['rate'] if c==10 else style['text'])
        weighted=sum(2 if ord(ch)>255 else 1 for ch in str(values[10]))
        sheet.height(row,max(42,min(250,15*(weighted//75+1))))
    columns=ET.Element(M+'cols')
    widths=[28,16,10,38,24,22,10,18,18,20,80,16,75]
    for c,width in enumerate(widths,1):ET.SubElement(columns,M+'col',min=str(c),max=str(c),width=str(width),customWidth='1')
    sheet.insert_element(columns)
    views=ET.Element(M+'sheetViews');view=ET.SubElement(views,M+'sheetView',workbookViewId='0',showGridLines='0')
    ET.SubElement(view,M+'pane',ySplit='3',topLeftCell='A4',activePane='bottomLeft',state='frozen')
    sheet.insert_element(views)
    last=max(4,3+len(unique))
    if unique:
        sheet.insert_element(ET.Element(M+'autoFilter',ref=f'A3:M{last}'))
        validations=ET.Element(M+'dataValidations',count='1')
        rule=ET.SubElement(validations,M+'dataValidation',type='decimal',operator='between',allowBlank='1',showErrorMessage='1',errorTitle='利率格式',error='请输入0%到25%之间的百分数，或清空表示缺失。',sqref=f'J4:J{last}')
        ET.SubElement(rule,M+'formula1').text='0';ET.SubElement(rule,M+'formula2').text='0.25'
        sheet.insert_element(validations)
    sheet.sync()
    return last

def validate_links(book):
    """Allow only our local source formulas; inspect their cached values too."""
    detail=book.sheets.get(DETAIL_SHEET)
    if detail is None:return []
    records={}
    for row in range(4,detail.maxrow+1):
        key=detail.value(row,1)
        if key is None:continue
        if key in records:raise ValueError('Premier 明细记录编号重复')
        if not re.fullmatch('[a-f0-9]{24}',str(key)):raise ValueError('Premier 明细记录编号无效')
        records[key]=detail.value(row,10)
    last=max(4,detail.maxrow);checks=[]
    for (r,c),cell in book.sheets['Premier Rates'].cells.items():
        formula=cell.findtext(M+'f')
        if formula is None:
            if c>=4 and numeric(book.sheets['Premier Rates'].value(r,c)):
                raise ValueError('Premier 利率被转换为固定数字：'+address(r,c))
            continue
        match=re.search(r'MATCH\("([a-f0-9]{24})",',formula)
        # Excel may omit optional quotes around this Chinese sheet name when
        # users save a workbook. Keep validating the exact local formula.
        normal=lambda value:value.replace("'"+DETAIL_SHEET+"'!",DETAIL_SHEET+'!')
        if not match or normal(formula)!=normal(reference_formula(match[1],last)) or match[1] not in records:
            raise ValueError('Premier 公式未引用本期专属明细：'+address(r,c))
        value=records[match[1]];expected=value if numeric(value) else '-'
        if book.sheets['Premier Rates'].value(r,c)!=expected:raise ValueError('Premier 公式缓存与明细不一致：'+address(r,c))
        checks.append(dict(address=address(r,c),key=match[1],formula=formula))
    return checks

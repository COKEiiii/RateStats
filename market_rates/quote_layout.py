"""Extend a quotation layout in the plan, leaving original assets immutable."""
import re


def tenor_order(value):
    m=re.fullmatch(r'(\d+)([MD])',value)
    return (int(m[1])*(30 if m[2]=='M' else 1),value)


def extend_sgd(snap, groups, sheet):
    known={str(v['title']) for v in snap['rainbow'].values()}
    missing=sorted({g['display_tenor'] for g in groups}-known,key=tenor_order)
    sections=[];last=max(x['row'] for x in snap['report']);nextcol=max(map(int,snap['rainbow']),default=1)+3
    for t in missing:
        start=last+3
        banks=sorted({g['bank'] for g in groups if g['display_tenor']==t})
        label=t[:-1]+(' Months' if t.endswith('M') else ' Days')
        entries=[(start,[label,None,None]),(start+1,['日期',None,None]),(start+2,[None,'全部公开报价',None])]
        entries += [(start+3+i,[bank,None,None]) for i,bank in enumerate(banks)]
        snap['report'].extend(dict(row=r,values=v) for r,v in entries)
        sheet[f'A{start+1}']='日期'
        sections.append(dict(tenor=t,start=start,banks=banks))
        last=start+2+len(banks)
        snap['rainbow'][str(nextcol)]=dict(title=t,rows=[],merges=[])
        nextcol+=3
    return sections


def prepare_sgd_layout(template, sections, output):
    """Append empty styled sections without copying Excel merges or history.

    Only styles and labels are added here. Excel still owns all subsequent
    row/column insertion, reference adjustment, calculation and saving.
    """
    from .report_xml import Book
    book=Book(template);sheet=book.sheets['SGD促销']
    for section in sections:
        start=section['start']
        labels=[section['tenor'][:-1]+(' Months' if section['tenor'].endswith('M') else ' Days'),'日期',None]+section['banks']
        for offset,label in enumerate(labels):
            refrow=2 if offset==0 else 3 if offset==1 else 4 if offset==2 else 5
            for c in (1,2,3):
                source=sheet.cells.get((refrow,c))
                sheet.put(start+offset,c,None,source.get('s','0') if source is not None else '0')
            if label is not None:sheet.put(start+offset,1,label)
        sheet.put(start+2,2,'全部公开报价')
    book.save(output)


def prepare_board_layout(template, histories, output):
    """Add empty new tenor sections; no previous quote values are copied."""
    import xml.etree.ElementTree as ET
    from .report_xml import Book,M,address
    book=Book(template)
    for history in histories:
        sheet=book.sheets[history['sheet']];bc=history['bank_col'];cc=history['current_col']
        header=history['old_date_rows'][0];sample=header+1
        for section in history.get('extensions',[]):
            start=section['start']
            for row in range(start,start+2+len(section['rows'])):
                ref=header-1 if row==start else header if row==start+1 else sample
                for c in range(bc,cc+1):
                    source=sheet.cells.get((ref,c));sheet.put(row,c,None,source.get('s','0') if source is not None else '0')
                sheet.height(row,24 if row<=start+1 else 80)
            sheet.put(start,bc,section['tenor']);sheet.put(start+1,bc,'银行');sheet.put(start+1,bc+1,'全部公开报价')
            for item in section['rows']:
                if item['bank']:sheet.put(item['row'],bc,item['bank'])
                sheet.put(item['row'],bc+1,item['amount'])
            for bank in section['banks']:
                if bank['end']<=bank['start']:continue
                merges=sheet.root.find(M+'mergeCells')
                if merges is None:merges=ET.Element(M+'mergeCells');sheet.insert_element(merges)
                ET.SubElement(merges,M+'mergeCell',ref=address(bank['start'],bc)+':'+address(bank['end'],bc))
                merges.set('count',str(len(merges)))
    book.save(output)

"""macOS Excel automation operations, backed by xlwings and Microsoft Excel."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sys
import uuid
from typing import Iterator
from decimal import Decimal

# PyPI's appscript wheel intentionally keeps its packages beneath ``aeosa``.
# Add that directory explicitly so xlwings' macOS engine can import ``aem``.
if sys.platform == "darwin":
    site_packages = Path(__import__("site").getsitepackages()[0])
    appscript_packages = site_packages / "aeosa"
    if appscript_packages.is_dir():
        sys.path.insert(0, str(appscript_packages))

import xlwings as xw
from appscript import k as appscript_kw, mactypes


@contextmanager
def excel_app() -> Iterator[xw.App]:
    app = xw.App(visible=False, add_book=False)
    pid = app.pid
    try:
        app.display_alerts = False
        app.enable_events = False
        app.screen_updating = False
        app.calculation = "manual"
        yield app
    finally:
        # Only shut down the private instance created by this invocation.
        try:
            app.quit()
        except Exception:
            # xw.App above created this private instance. Never terminate an
            # existing user's Excel when an Apple Event cleanup fails.
            try:app.kill()
            except Exception:pass


def open_book(app: xw.App, path: str | Path, *, readonly: bool = False) -> xw.Book:
    resolved=Path(path).resolve()
    # Pass a native file reference, including for Chinese names and spaces.
    app.api.open_workbook(workbook_file_name=mactypes.File(str(resolved)),
                          update_links=appscript_kw.do_not_update_links,
                          read_only=readonly,timeout=60)
    return app.books[resolved.name]


def save_as(book: xw.Book, path: str | Path) -> None:
    target = Path(path).resolve()
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing workbook: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Excel's AppleScript Save As rejects some valid Unicode basenames with
    # OS error -50. Save under a short ASCII name beside the target, close it,
    # then atomically give the completed workbook its requested Unicode name.
    staging = target.parent / f"codex-stage-{uuid.uuid4().hex}.xlsx"
    book.save(str(staging))
    book.close()
    os.replace(staging, target)


def copied_book(app: xw.App, template: str | Path, target: str | Path) -> xw.Book:
    """Open a private copy directly, avoiding Excel Save As path limitations."""
    import shutil
    target = Path(target).resolve()
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing workbook: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(template).resolve(), target)
    return open_book(app, target)


def cell(sheet, row: int, col: int):
    # Tuple coordinates make xlwings ask Excel for each cell's address first.
    # A1 addresses identify the same cell without that extra Apple Event.
    return sheet.range(f"{xw.utils.col_name(col)}{row}")


def set_formula(target, value) -> None:
    """Matrix writes avoid Mac Excel silently ignoring long scalar formulas."""
    target.formula=[[value]] if isinstance(value,str) else value


def plan_array_value(value):
    """Preserve literal placeholders in mixed formula/value matrix writes."""
    return "'"+value if isinstance(value,str) and not value.startswith('=') else value


def set_value(target, value) -> None:
    if isinstance(value, str) and value.startswith("="):
        set_formula(target,value)
    elif value is None:
        target.clear_contents()
    else:
        target.value = float(value) if isinstance(value, (int, float)) else str(value)


def check_value(actual, expected, label: str) -> None:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if actual is None or isinstance(actual, str) or abs(float(actual) - float(expected)) > 1e-10:
            raise AssertionError(f"{label}: expected {expected}, got {actual}")
    elif str(actual or "") != str(expected or ""):
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def _excel_color(hex_color: str) -> int:
    value=hex_color.lstrip("#")
    r,g,b=(int(value[i:i+2],16) for i in (0,2,4))
    return r+256*g+65536*b


def assign_cells(sheet, entries, expected_key="expected", number_format="0.0000%") -> None:
    for item in entries:
        dest = sheet.range(item["address"])
        set_value(dest, item.get("formula", item.get("value")))
        if item.get(expected_key) is not None and isinstance(item[expected_key], (int, float)):
            dest.number_format = number_format


def merge(sheet, r1: int, c1: int, r2: int, c2: int):
    area = sheet.range((r1, c1), (r2, c2))
    area.api.unmerge()
    if (r1, c1) != (r2, c2):
        anchor = cell(sheet, r1, c1)
        saved = anchor.formula if isinstance(anchor.formula, str) and anchor.formula.startswith("=") else anchor.value
        # Excel can display a modal warning even when only the anchor has a
        # value. Merge a fully empty range, then restore that value/formula.
        area.clear_contents()
        area.api.merge()
        if saved is not None:
            set_value(anchor, saved)
    return area


def used_last(sheet, axis="row") -> int:
    used = sheet.used_range
    return used.row + used.rows.count - 1 if axis == "row" else used.column + used.columns.count - 1


def insert_rows(sheet, start: int, count: int, *, last_col: int | None = None) -> None:
    """Insert rows through xlwings, optionally limiting shifts to used columns."""
    if count > 0:
        if last_col is None:
            area = sheet.range(f"{start}:{start + count - 1}")
        else:
            area = sheet.range((start, 1), (start + count - 1, last_col))
        area.insert(shift="down")


def insert_columns(sheet, start: int, count: int = 1) -> None:
    """Insert whole columns through xlwings' cross-platform range API."""
    if count > 0:
        first=xw.utils.col_name(start)
        last=xw.utils.col_name(start + count - 1)
        sheet.range(f"{first}:{last}").insert(shift="right")


def copy_formats(source, destination) -> None:
    """Copy formatting to a concrete range while retaining destination contents."""
    contents = destination.formula
    source.copy(destination=destination)
    set_formula(destination,contents)


def inserted_column_copy_segments(template: Path, sheet_name: str, column: int, last_row: int):
    """Exclude horizontal title merges that make Mac Excel reject a range copy."""
    from openpyxl import load_workbook
    book=load_workbook(template,read_only=False,data_only=False)
    try:
        blocked=set()
        for area in book[sheet_name].merged_cells.ranges:
            if area.min_col<=column<=area.max_col and area.min_col<area.max_col:
                blocked.update(range(area.min_row,min(area.max_row,last_row)+1))
    finally:book.close()
    start=None
    for row in range(1,last_row+2):
        if row<=last_row and row not in blocked:
            if start is None:start=row
        elif start is not None:
            yield start,row-1
            start=None


def clear_conditional_formats(area) -> None:
    """Delete native conditions individually; empty collections reject Delete on Mac."""
    conditions=area.api.format_conditions.get()
    if conditions is None or conditions==appscript_kw.missing_value:
        return
    for condition in reversed(conditions):
        condition.delete()


def export_preview(sheet, area: str, target: Path, *, save_source: bool = True) -> None:
    """Export a range through a one-sheet, disposable Excel workbook."""
    import shutil
    import subprocess
    target=Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    app=sheet.book.app;source=Path(sheet.book.fullname).resolve()
    # Keep all transient workbook/PDF files beside the project workbook. macOS
    # Excel prompts for sandbox access when automation exports under /tmp/T.
    token=uuid.uuid4().hex;temp_root=source.parent
    staging=temp_root/f"codex-preview-{token}.xlsx";pdf_stage=temp_root/f"codex-preview-{token}.pdf"
    profile=None
    if save_source:sheet.book.save()
    shutil.copy2(source,staging)
    temp_book=None
    try:
        temp_book=open_book(app,staging)
        preview=temp_book.sheets[sheet.name]
        # Freeze cached values before deleting historic and source sheets.
        used=preview.used_range
        # Assigning the evaluated values freezes formulas without Excel's
        # clipboard-backed Copy/Paste commands.
        used.value=used.value
        other_names=[other.name for other in temp_book.sheets if other.name!=preview.name]
        for name in other_names:temp_book.sheets[name].delete()
        preview.page_setup.print_area=area
        # xlwings exposes only print_area through the macOS wrapper. Set the
        # optional native page settings through appscript when available.
        api=preview.page_setup.api;touched=[]
        for name,value in (("page_orientation",xw.constants.PageOrientation.xlLandscape),
                           ("zoom",False),("fit_to_pages_wide",1),("fit_to_pages_tall",1)):
            try:
                prop=getattr(api,name);old=prop.get();prop.set(value);touched.append((prop,old))
            except Exception:
                continue
        # A configured printer is required by Excel's macOS PDF/print pipeline.
        # On printerless Macs, Excel displays a modal print error and blocks
        # Apple Events. Use the bundled/installed LibreOffice converter for
        # this disposable, values-only preview workbook instead.
        printer_check=subprocess.run(["lpstat","-d"],capture_output=True,text=True,
                                     env={**os.environ,"LC_ALL":"C"},timeout=10)
        printer_status=(printer_check.stdout+"\n"+printer_check.stderr).lower()
        has_default_printer=("system default destination:" in printer_status and
                             "no system default destination" not in printer_status and
                             "无系统默认目的位置" not in printer_status and
                             "未添加目的位置" not in printer_status)
        if printer_check.returncode==0 and has_default_printer:
            preview.to_pdf(str(pdf_stage))
        else:
            from scripts.platform_runtime import pdf_converter
            converter=pdf_converter()
            if not converter:
                raise RuntimeError("No default macOS printer is configured and LibreOffice 'soffice' is unavailable for PDF preview export")
            temp_book.save()
            temp_book.close()
            # Configure the disposable file via the lossless XML editor: the
            # macOS PageSetup API does not expose all these print properties.
            import xml.etree.ElementTree as ET
            from market_rates.report_xml import Book, M
            document=Book(staging);copy_sheet=document.sheets[sheet.name]
            properties=copy_sheet.root.find(M+"sheetPr")
            if properties is None:
                properties=ET.Element(M+"sheetPr");copy_sheet.insert_element(properties)
            fit=properties.find(M+"pageSetUpPr")
            if fit is None:fit=ET.SubElement(properties,M+"pageSetUpPr")
            fit.set("fitToPage","1")
            setup=copy_sheet.root.find(M+"pageSetup")
            if setup is None:
                setup=ET.Element(M+"pageSetup");copy_sheet.insert_element(setup)
            setup.attrib.pop("scale",None)
            setup.attrib.update(orientation="landscape",paperSize="9",fitToWidth="1",fitToHeight="1")
            document.save(staging)
            profile=source.parent/f".codex-lo-profile-{token}"
            profile.mkdir()
            # Bundled LibreOffice uses fontconfig rather than Core Text. Give
            # it Mac fonts explicitly so Chinese glyphs are rendered/embedded.
            font_config=profile/"fonts.conf"
            fonts=ET.Element("fontconfig")
            for directory in (Path("/System/Library/Fonts"),Path("/System/Library/Fonts/Supplemental"),
                              Path("/Library/Fonts"),Path.home()/"Library/Fonts"):
                if directory.is_dir():ET.SubElement(fonts,"dir").text=str(directory)
            ET.SubElement(fonts,"cachedir").text=str(profile/"font-cache")
            for family,replacement in (("SimSun","Songti SC"),("宋体","Songti SC"),
                                       ("DengXian","Heiti SC"),("等线","Heiti SC")):
                alias=ET.SubElement(fonts,"alias");ET.SubElement(alias,"family").text=family
                ET.SubElement(ET.SubElement(alias,"prefer"),"family").text=replacement
            font_config.write_bytes(ET.tostring(fonts,encoding="utf-8",xml_declaration=True))
            profile_uri=profile.as_uri()
            subprocess.run([converter,f"-env:UserInstallation={profile_uri}","--headless","--convert-to","pdf","--outdir",str(temp_root),str(staging)],check=True,capture_output=True,text=True,timeout=180,env={**os.environ,"FONTCONFIG_FILE":str(font_config)})
        os.replace(pdf_stage,target)
    finally:
        try:
            if temp_book is not None:temp_book.close()
        except Exception:pass
        staging.unlink(missing_ok=True)
        pdf_stage.unlink(missing_ok=True)
        if profile is not None:shutil.rmtree(profile,ignore_errors=True)


def test_cell(sheet, row: int, col: int, expected) -> None:
    check_value(cell(sheet, row, col).value, expected, f"{sheet.name} {row}/{col}")


def check_pending_cells(pending):
    """Read each native result area once, retaining every numeric assertion."""
    grouped={}
    for sheet,row,column,expected in pending:
        # Worksheet.name is an Apple Event on macOS.  Group the live objects
        # locally so large batches do not spend minutes rereading the same
        # name for every assertion.
        entry=grouped.setdefault(id(sheet),dict(sheet=sheet,checks=[]))
        entry['checks'].append((row,column,expected))
    for entry in grouped.values():
        sheet=entry['sheet'];checks=entry['checks'];name=sheet.name
        top=min(r for r,c,e in checks);bottom=max(r for r,c,e in checks)
        left=min(c for r,c,e in checks);right=max(c for r,c,e in checks)
        values=sheet.range((top,left),(bottom,right)).options(ndim=2).value
        for row,column,expected in checks:
            check_value(values[row-top][column-left],expected,f'{name} {row}/{column}')


def write_sparse_formulas(sheet,column,updates):
    """Keep section headers while writing one comparison column in one event."""
    if not updates:return
    first,last=min(updates),max(updates)
    area=sheet.range((first,column),(last,column));values=area.formula
    if not isinstance(values,list):values=[[values]]
    elif values and not isinstance(values[0],list):values=[[v] for v in values]
    for row,formula in updates.items():values[row-first][0]=formula
    set_formula(area,values);area.number_format='0.00%'


def style_new_sgd_rainbow_groups(sheet,groups):
    """New tenor triples inherit a complete usable layout, not default widths."""
    new=[group for group in groups if int(group['col'])>21]
    if not new:return
    sample=clone_sheet(sheet,sheet.book,'_sgd_style_temp')
    try:
        sample.used_range.api.unmerge()
        for group in new:
            col=int(group['col']);last=int(group['last_row'])
            copy_formats(sample.range('A2:C2'),sheet.range((2,col),(2,col+2)))
            for offset,width in enumerate((19,22,50)):sheet.api.columns[col+offset].column_width.set(width)
            sheet.range((3,col),(last,col+2)).api.unmerge()
            for row in range(3,last+1):copy_formats(sample.range('A3:C3'),sheet.range((row,col),(row,col+2)))
            area=sheet.range((3,col),(last,col+2));area.wrap_text=True;area.api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
            from market_rates.report_format import fitted_height
            for row in range(3,last+1):
                height=fitted_height([cell(sheet,row,col+2).value],[50],72)
                sheet.api.rows[row].row_height.set(max(float(cell(sheet,row,col).row_height or 0),height))
            for label in group.get('bank_labels',[]):
                merge(sheet,int(label['start']),col,int(label['end']),col);cell(sheet,int(label['start']),col).value=label['bank']
    finally:sample.delete()


def restore_sgd_rainbow_rate_fills(sheet,groups,template):
    """Restore the template rate fill on inserted rows in existing tenor groups."""
    from openpyxl import load_workbook
    book=load_workbook(template,data_only=False,read_only=False)
    try:
        baseline=book['SGD Promotional Rate']
        for group in groups:
            col=int(group['col'])
            if col>baseline.max_column:continue
            sample=baseline.cell(3,col+1).fill
            color=sample.fgColor
            if sample.fill_type!='solid' or color.type!='rgb' or not color.rgb:continue
            hex_color=color.rgb[-6:]
            rgb=tuple(int(hex_color[i:i+2],16) for i in (0,2,4))
            for block in group['blocks']:
                if not block.get('group'):continue
                for offset in range(len(block['group']['details'])):
                    sheet.range((int(block['target_start'])+offset,col+1)).color=rgb
    finally:book.close()


def put(sheet, entries) -> None:
    for item in entries:
        dest = sheet.range(item["address"])
        set_value(dest, item.get("formula"))
        if isinstance(item.get("expected"), (float, int)):
            dest.number_format = "0.0000%"


def put_batched(sheet, entries, address_key="address") -> None:
    """Apply sparse cell updates one Excel row at a time, not per cell."""
    from collections import defaultdict
    rows=defaultdict(dict)
    for item in entries:
        match=re.fullmatch(r"([A-Z]+)(\d+)",item[address_key])
        letters,row=match.groups();number=0
        for ch in letters:number=number*26+ord(ch)-64
        rows[int(row)][number]=item.get("value")
    for row,updates in rows.items():
        first,last=min(updates),max(updates)
        area=sheet.range((row,first),(row,last))
        current=area.formula
        if not isinstance(current,list):current=[[current]]
        elif current and not isinstance(current[0],list):current=[current]
        values=list(current[0])
        for column,value in updates.items():values[column-first]=value
        set_formula(area,[values])


def contiguous_update_spans(entries, address_key="final_address"):
    """Group sparse updated cells into exact row spans for Excel formatting."""
    from collections import defaultdict
    rows=defaultdict(set)
    for item in entries:
        match=re.fullmatch(r"([A-Z]+)(\d+)",item[address_key])
        if not match:raise ValueError(f"Invalid cell address: {item[address_key]}")
        column=0
        for char in match.group(1):column=column*26+ord(char)-64
        rows[int(match.group(2))].add(column)
    for row in sorted(rows):
        columns=sorted(rows[row]);start=previous=columns[0]
        for column in columns[1:]:
            if column!=previous+1:
                yield row,start,previous
                start=column
            previous=column
        yield row,start,previous


def clone_sheet(sheet, book, name: str):
    copied = sheet.copy(after=book.sheets[-1])
    copied.name = name
    copied.used_range.api.unmerge()
    return copied


def action_board(plan_path: Path, writer="board") -> None:
    from market_rates.common import save
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    out = plan_path.parent
    checks = []
    with excel_app() as app:
        inputs = open_book(app, out / "board-inputs.xlsx", readonly=True)
        try:
            for kind in ("report", "rainbow"):
                template=Path(plan[f"{kind}_template"]);prepared=None
                if kind=='report' and any(h.get('extensions') for h in plan['histories']):
                    from market_rates.quote_layout import prepare_board_layout
                    prepared=out/('layout-'+uuid.uuid4().hex+'.xlsx')
                    prepare_board_layout(template,plan['histories'],prepared);template=prepared
                try:book = copied_book(app, template, plan[f"{kind}_output"])
                finally:
                    if prepared is not None:prepared.unlink(missing_ok=True)
                try:
                    pending_checks=[]
                    inputs.sheets[0].copy(after=book.sheets[-1])
                    if book.sheets[-1].name != plan["input_sheet"]:
                        raise ValueError("Input sheet name collision")
                    if kind == "report":
                        for matrix in plan["matrices"]:
                            print('Board matrix: '+matrix['sheet'],flush=True)
                            sheet = book.sheets[matrix["sheet"]]
                            sample = clone_sheet(sheet, book, "_wide_style_temp")
                            bc, ac, nc = int(matrix["bank_col"]), int(matrix["amount_col"]), int(matrix["note_col"])
                            for h in matrix.get('extra_headers',[]):
                                copy_formats(cell(sheet,2,ac+1),cell(sheet,2,h['col']))
                                cell(sheet,2,h['col']).value=h['tenor'];sheet.api.columns[h['col']].column_width.set(14)
                            for insert in sorted(matrix["inserts"], key=lambda x: int(x["before"]), reverse=True):
                                insert_rows(sheet,int(insert['before']),int(insert['count']))
                            lastcol=max([nc]+[h['col'] for h in matrix.get('extra_headers',[])])
                            block_area=sheet.range((3,bc),(int(matrix["end"]),lastcol));block_area.api.unmerge();block_area.clear_contents()
                            for group in matrix["groups"]:
                                row = int(group["target_row"])
                                first = row
                                for item in group["rows"]:
                                    src = int(item["source_row"])
                                    sample.range((src, bc), (src, nc)).copy(destination=sheet.range((row, bc), (row, nc)))
                                    values=[None]*(lastcol-bc+1)
                                    values[ac-bc]="'"+str(item['amount']);values[nc-bc]="'"+str(item['note'])
                                    pending_checks.extend([(sheet,row,ac,str(item['amount'])),(sheet,row,nc,str(item['note']))])
                                    for entry in item["values"]:
                                        values[int(entry['col'])-bc]=plan_array_value(entry.get('formula'))
                                        pending_checks.append((sheet,row,int(entry["col"]),entry["expected"]))
                                    set_formula(sheet.range((row,bc),(row,lastcol)),[values])
                                    sheet.range((row,ac+1),(row,nc-1)).number_format='0.0000%'
                                    if lastcol>nc:sheet.range((row,nc+1),(row,lastcol)).number_format='0.0000%'
                                    sheet.range((row, ac), (row, nc)).wrap_text = True
                                    sheet.api.rows[row].row_height.set(60)
                                    row += 1
                                merge(sheet, first, bc, row - 1, bc)
                                cell(sheet, first, bc).value = group["label"]
                            sheet.api.columns[ac].column_width.set(36)
                            sheet.api.columns[nc].column_width.set(58)
                            sheet.range((3, bc), (int(matrix["end"]), nc)).font.size = 10
                            sample.delete()
                            cell(sheet, 1, bc).value = f"{matrix['currency']}挂牌 {plan['as_of']}；银行来源日期及待核项见本期明细"
                            date_col = 15 if matrix["currency"] == "SGD" else 10
                            heading = cell(sheet, 1, date_col)
                            heading.value = datetime.fromisoformat(plan["as_of"])
                            heading.number_format = "yyyy-mm-dd"
                        for history in plan["histories"]:
                            print('Board history: '+history['sheet'],flush=True)
                            sheet = book.sheets[history["sheet"]]
                            cc, bc = int(history["current_col"]), int(history["bank_col"])
                            # Excel's Rows object spans all 16,384 columns. Copying
                            # one whole row for every inserted history tier makes
                            # macOS Excel redraw and process millions of irrelevant
                            # cells. Restrict the format copy to this sheet's used
                            # columns while preserving the complete visible layout.
                            used_columns = int(sheet.used_range.last_cell.column)
                            sheet.api.columns[bc + 1].column_width.set(46)
                            if history["insert_date"]:
                                insert_columns(sheet,cc)
                                used_columns+=1
                                # CNY has title and section rows merged across
                                # both columns. Mac Excel rejects a range copy
                                # crossing those partial merges with OSERROR -50.
                                # Insertion already extends their formatting, so
                                # only copy the unmerged row segments.
                                for first, last in inserted_column_copy_segments(
                                    Path(plan["report_template"]), history["sheet"], cc,
                                    int(history["old_max_row"]),
                                ):
                                    copy_formats(
                                        sheet.range((first, cc + 1), (last, cc + 1)),
                                        sheet.range((first, cc), (last, cc)),
                                    )
                                for row in range(1, int(history["old_max_row"]) + 1):
                                    if not cell(sheet, row, cc).api.merge_cells.get(): cell(sheet, row, cc).value = "-"
                            for item in sorted(history["inserts"], key=lambda x: int(x["before"]), reverse=True):
                                before, count, anchor = int(item["before"]), int(item["count"]), int(item["anchor"])
                                # History data and formatting are contained in the
                                # used columns. Inserting full Excel rows forces
                                # Excel to shift 16,384 cells for each tier; insert
                                # the complete used width instead to keep native
                                # reference adjustment while avoiding blank cells.
                                insert_rows(sheet,before,count,last_col=used_columns)
                                for offset in range(count):
                                    destrow = before + offset
                                    # The bank column is merged across history
                                    # tiers (A for CNY, B for USD). Copying a
                                    # range that includes part of that merge is
                                    # rejected by Mac Excel with OSERROR -50.
                                    # Excel's row insertion already carries the
                                    # bank-column border; copy the unmerged areas.
                                    for first_col, last_col in ((1, bc - 1), (bc + 1, used_columns)):
                                        if first_col > last_col:
                                            continue
                                        source_row = sheet.range((anchor, first_col), (anchor, last_col))
                                        destination_row = sheet.range((destrow, first_col), (destrow, last_col))
                                        source_row.copy(destination=destination_row)
                                        destination_row.clear_contents()
                                    cell(sheet, destrow, bc + 1).value = str(item["rows"][offset]["amount"])
                                    dest = cell(sheet, destrow, cc); set_value(dest, item["rows"][offset]["formula"])
                                    dest.number_format = "0.0000%"
                                merge(sheet, anchor, bc, before + count - 1, bc)
                                cell(sheet, anchor, bc).value = item["label"]
                            for row in history["date_rows"]:
                                cell(sheet, int(row), cc).value = datetime.fromisoformat(plan["as_of"])
                                cell(sheet, int(row), cc).number_format = "yyyy-mm-dd"
                            for item in history['inserts']:
                                for offset,entry in enumerate(item['rows']):
                                    pending_checks.append((sheet,int(item['target_row'])+offset,cc,entry['expected']))
                            put(sheet, history["existing"])
                            for entry in history["existing"]:
                                target = sheet.range(entry["address"])
                                pending_checks.append((sheet,target.row,target.column,entry["expected"]))
                            cell(sheet, 1, bc).value = f"{history['currency']}挂牌 {plan['as_of']}；原CNH按CNY；新增条件在本银行内，历史保留"
                        sheet = book.sheets["最高报价汇总 "]
                        put(sheet, plan["summary"])
                        sheet.range("H41").value = plan["as_of"]
                        sheet.range("H58").value = plan["as_of"]
                        cell(sheet, 41, 2).value = "美元挂牌；覆盖及待核见本期明细"
                    else:
                        for name in ("SGD Board Rate", "USD Rate + Other Currency Rates"):
                            print('Board rainbow: '+name,flush=True)
                            sheet = book.sheets[name]
                            sample = clone_sheet(sheet, book, "_wide_style_temp")
                            if name != "SGD Board Rate" and int(plan["rainbow_shift"]):
                                start = int(plan["rainbow_insert_before"]); shift = int(plan["rainbow_shift"])
                                insert_rows(sheet,start,shift)
                            for block in (b for b in plan["rainbow_blocks"] if b["sheet"] == name):
                                col, start, end = int(block["col"]), int(block["start"]), int(block["end"])
                                header=int(block.get("header",start-1))
                                for offset,value in enumerate((block['tenor'],'Highest Rate','Amount')):cell(sheet,header,col+offset).value=value
                                block_area=sheet.range((start,col),(end,col+2));block_area.api.unmerge();block_area.clear_contents()
                                for group in block["groups"]:
                                    row = int(group["target_row"])
                                    values=[]
                                    for item in group["rows"]:
                                        source = int(item["source_row"])
                                        stylecol=int(block.get("style_col",col))
                                        sample.range((source, stylecol), (source, stylecol + 2)).copy(destination=sheet.range((row, col), (row, col + 2)))
                                        values.append([None,plan_array_value(item['formula']),"'"+str(item['amount'])])
                                        pending_checks.append((sheet,row,col+1,item["expected"]))
                                        pending_checks.append((sheet,row,col+2,str(item['amount'])))
                                        row += 1
                                    first=int(group['target_row'])
                                    set_formula(sheet.range((first,col),(row-1,col+2)),values)
                                    sheet.range((first,col+1),(row-1,col+1)).number_format='0.0000%'
                                    sheet.range((first,col),(row-1,col+2)).row_height=96
                                    merge(sheet, int(group["target_row"]), col, row - 1, col)
                                    cell(sheet, int(group["target_row"]), col).value = group["label"]
                            sample.delete()
                        fixed = book.sheets["USD Rate + Other Currency Rates"]
                        put(fixed, plan["rainbow_cells"])
                        fixed.range("A31").value = f"USD Board Rates {plan['as_of']}；范围及待核见明细"
                        title_row = int(plan["rainbow_cny_title"]) + int(plan["rainbow_shift"])
                        cell(fixed, title_row, 1).value = f"CNY Board Rates {plan['as_of']}；含CNH"
                        book.sheets["SGD Board Rate"].range("A1").value = f"SGD挂牌 {plan['as_of']}；范围及待核见明细"
                    app.calculate()
                    check_pending_cells(pending_checks)
                    if kind == "report":
                        for matrix in plan["matrices"]:
                            date_col = 15 if matrix["currency"] == "SGD" else 10
                            actual = cell(book.sheets[matrix["sheet"]], 1, date_col).value
                            if not hasattr(actual,"strftime") or actual.strftime("%Y-%m-%d") != plan["as_of"]:
                                raise AssertionError(f"{matrix['sheet']} date heading: {actual}")
                    else:
                        for entry in plan["rainbow_cells"]:
                            target = fixed.range(entry["address"])
                            check_value(target.value,entry["expected"],"Rainbow fixed "+entry["address"])
                    # Excel only updates the working copy; templates and source books stay untouched.
                    book.save()
                    checks.append({"kind": kind, "passed": True, "sheet_count": len(book.sheets)})
                    book.close()
                except BaseException:
                    try: book.close()
                    except Exception: pass
                    raise
        finally:
            inputs.close()
    save(out / "native-checks.json", checks)


def action_board_repair(plan_path: Path) -> None:
    """Finish omitted fixed cells in already saved Mac board workbooks."""
    from market_rates.table_validation import sha
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    for kind in ("report", "rainbow"):
        if sha(plan[f"{kind}_template"]) != plan[f"{kind}_sha256"]:
            raise ValueError(f"{kind} template changed since board planning")
    with excel_app() as app:
        for kind in ("report", "rainbow"):
            book = open_book(app, plan[f"{kind}_output"])
            try:
                if kind == "report":
                    for matrix in plan["matrices"]:
                        sheet = book.sheets[matrix["sheet"]]
                        date_col = 15 if matrix["currency"] == "SGD" else 10
                        heading = cell(sheet, 1, date_col)
                        heading.value = datetime.fromisoformat(plan["as_of"])
                        heading.number_format = "yyyy-mm-dd"
                    summary = book.sheets["最高报价汇总 "]
                    summary.range("H41").value = plan["as_of"]
                    summary.range("H58").value = plan["as_of"]
                else:
                    sheet = book.sheets["USD Rate + Other Currency Rates"]
                    put(sheet, plan["rainbow_cells"])
                    sheet.range("A31").value = f"USD Board Rates {plan['as_of']}；范围及待核见明细"
                    title_row = int(plan["rainbow_cny_title"]) + int(plan["rainbow_shift"])
                    cell(sheet, title_row, 1).value = f"CNY Board Rates {plan['as_of']}；含CNH"
                app.calculate()
                if kind == "report":
                    for matrix in plan["matrices"]:
                        date_col = 15 if matrix["currency"] == "SGD" else 10
                        actual = cell(book.sheets[matrix["sheet"]], 1, date_col).value
                        if not hasattr(actual, "strftime") or actual.strftime("%Y-%m-%d") != plan["as_of"]:
                            raise AssertionError(f"{matrix['sheet']} date heading: {actual}")
                else:
                    for entry in plan["rainbow_cells"]:
                        check_value(sheet.range(entry["address"]).value, entry["expected"],
                                    "Rainbow fixed " + entry["address"])
                book.save()
            finally:
                book.close()
    for kind in ("report", "rainbow"):
        if sha(plan[f"{kind}_template"]) != plan[f"{kind}_sha256"]:
            raise AssertionError(f"{kind} template changed during board repair")


def action_scrub_sgd(manifest_path: Path) -> None:
    """Clear prior-date quote tails in a private delivery copy using Excel."""
    from market_rates.common import save
    from market_rates.report_xml import Book, M, pos
    from market_rates.table_validation import sha
    document=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    path=Path(document['workbook']).resolve()
    if sha(path)!=document['input_sha256']:
        raise ValueError('SGD delivery copy changed before old-date cleanup')
    with excel_app() as app:
        book=open_book(app,path)
        try:
            sheet=book.sheets['SGD Promotional Rate']
            for entry in document['cells']:
                target=sheet.range(entry['cell'])
                formula=str(target.formula or '')
                if not all('明细_'+day in formula for day in entry['source_dates']):
                    raise ValueError('SGD quote source changed: '+entry['cell'])
                target.clear_contents()
                target.offset(0,1).clear_contents()
            app.calculate()
            book.save()
        finally:
            book.close()
    saved=Book(path).sheets['SGD Promotional Rate']
    for entry in document['cells']:
        row,column=pos(entry['cell'])
        if saved.value(row,column) is not None or saved.cells[row,column].find(M+'f') is not None:
            raise AssertionError('SGD stale quote remains: '+entry['cell'])
        if saved.value(row,column+1) is not None:
            raise AssertionError('SGD stale quote caption remains: '+entry['cell'])
    document.update(status='complete',output_sha256=sha(path))
    save(manifest_path,document)


def action_check_delivery(manifest_path: Path, out: Path) -> None:
    import hashlib
    import shutil
    from market_rates.common import save
    document = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    results = []
    with excel_app() as app:
        for kind, original in document["files"].items():
            path = Path(original).resolve()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            copy = out.parent / (kind + "-readonly-check-" + uuid.uuid4().hex + ".xlsx")
            if copy.exists(): raise FileExistsError(f"Fresh check directory required: {copy}")
            shutil.copy2(path, copy)
            copy.chmod(0o444)
            book = open_book(app, copy, readonly=True)
            try:
                if not book.api.read_only.get(): raise AssertionError(f"Read only open failed: {kind}")
                results.append({"name": kind, "path": str(path), "sheet_count": len(book.sheets), "passed": True})
            finally: book.close()
            if hashlib.sha256(path.read_bytes()).hexdigest() != before: raise AssertionError(f"Original changed: {kind}")
            copy.chmod(0o644); copy.unlink()
    save(out, {"passed": True, "file_count": len(results), "checks": results})


def action_premier_checks(manifest_path: Path, out: Path) -> None:
    """Exercise editable Premier links only in uniquely named disposable copies."""
    from market_rates.common import save
    from market_rates.table_validation import sha
    document=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    out=out.resolve();out.parent.mkdir(parents=True,exist_ok=True)
    results=[]
    with excel_app() as app:
        for item in document["files"]:
            original=Path(item["path"]).resolve();before=sha(original)
            copy=out.parent/f"codex-premier-{uuid.uuid4().hex}.xlsx"
            book=copied_book(app,original,copy)
            try:
                premier=book.sheets["Premier Rates"];detail=book.sheets["本期专属明细"]
                cases=item["cells"]
                if len({case["key"] for case in cases})<2:
                    raise ValueError("Premier regression requires two distinct record keys")
                def verify(label,exclude=None):
                    for case in cases:
                        if case["key"]==exclude:continue
                        target=premier.range(case["address"])
                        if not str(target.formula).startswith("="):raise AssertionError("Premier formula missing: "+case["address"])
                        check_value(target.value,case["value"],label+" "+case["address"])
                app.calculate();verify("Initial")
                first=cases[0];row=int(first["row"])
                second=next(case for case in cases if case["key"]!=first["key"]);other=int(second["row"])
                source=detail.range((row,10));target=premier.range(first["address"]);old=source.value
                for value in (0.01234,0.0):
                    source.value=value;app.calculate();check_value(target.value,value,"Edited J rate")
                    verify("Unrelated customer",exclude=first["key"])
                source.clear_contents();app.calculate();check_value(target.value,"-","Blank J rate")
                source.value=old
                left=detail.range((row,1),(row,13));right=detail.range((other,1),(other,13))
                values_left,values_right=left.value,right.value
                left.value=values_right;right.value=values_left;app.calculate();verify("Reordered records")
                left.value=values_left;right.value=values_right
                detail.range((other,1)).value=first["key"];app.calculate()
                check_value(target.value,"待核实","Duplicate record key")
                detail.range((other,1)).value=second["key"]
                detail.range((row,1)).clear_contents();app.calculate()
                check_value(target.value,"待核实","Missing record key")
                detail.range((row,1)).value=first["key"];app.calculate()
                book.save();book.close();book=open_book(app,copy,readonly=True)
                premier=book.sheets["Premier Rates"];verify("Saved/reopened")
                from market_rates.report_xml import Book
                from market_rates.premier_details import validate_links
                if len(validate_links(Book(copy)))!=len(cases):raise AssertionError('Saved Premier formula/source links changed')
                if item["kind"]=="formatted_report":
                    for name,area,filename in (("Premier Rates","A1:K18","premier-preview.pdf"),
                                               ("本期专属明细","A1:M8","premier-detail-preview.pdf")):
                        if name=='Premier Rates':area='A1:K'+str(used_last(book.sheets[name]))
                        export_preview(book.sheets[name],area,out.parent/filename,save_source=False)
                results.append(dict(kind=item["kind"],file=str(original),formula_count=len(cases),
                    rate_change=True,zero=True,blank=True,sorting=True,duplicate_key=True,missing_key=True,
                    separate_customers=True,saved_reopened=True,original_unchanged=sha(original)==before))
                if sha(original)!=before:raise AssertionError("Premier original changed: "+str(original))
            except Exception as exc:
                save(out,dict(passed=False,checks=results,error=str(exc),evidence=str(copy)))
                raise
            finally:book.close()
            copy.unlink()
    save(out,dict(passed=True,checks=results))
    print("Premier native checks passed: "+str(len(results))+" workbooks",flush=True)


def action_native_styles(plan_path: Path) -> None:
    import hashlib
    import openpyxl
    from market_rates.common import save
    plan=json.loads(plan_path.read_text(encoding="utf-8-sig"));out=plan_path.parent
    samples=json.loads((out/"style-samples.json").read_text(encoding="utf-8-sig"))
    if not samples:
        # A same-day fixture may contain no newly inserted style samples.
        # Still verify each style used by the untouched workbook sheets.
        excluded={'report':{'SGD促销','最高报价汇总 ','Premier Rates','本期专属明细'},
                  'rainbow':{'SGD Promotional Rate'}}
        for kind in ('report','rainbow'):
            source=openpyxl.load_workbook(plan[kind+'_template'],data_only=False)
            try:
                for sheet in source.worksheets:
                    if sheet.title in excluded[kind]:continue
                    styles={}
                    for row in sheet:
                        for item in row:
                            if item.value is None:continue
                            sample=styles.setdefault(item.style_id,dict(kind=kind,sheet=sheet.title,
                                source=item.coordinate,target=item.coordinate,represented_cells=0))
                            sample['represented_cells']+=1
                    samples.extend(styles.values())
            finally:source.close()
        if not samples:raise AssertionError('No representative native styles to validate')
        save(out/'style-samples.json',samples)
    results=[]
    def signature(cell):
        def color(c):
            if c is None:return None
            if c.type=="rgb":return c.rgb
            if c.type=="indexed":return c.indexed
            if c.type=="theme":return (c.theme,c.tint)
            return None
        return (cell.font.name,cell.font.sz,cell.font.bold,cell.font.italic,cell.font.underline,
                cell.font.strike,color(cell.font.color),cell.fill.fill_type,color(cell.fill.fgColor),
                cell.number_format,cell.alignment.horizontal,cell.alignment.vertical,
                cell.alignment.wrap_text,cell.alignment.shrink_to_fit,cell.alignment.indent,
                cell.alignment.text_rotation,cell.protection.locked,cell.protection.hidden,
                tuple((getattr(cell.border,side).style,color(getattr(cell.border,side).color))
                      for side in ("left","right","top","bottom")))
    for kind in ("report","rainbow"):
        source=openpyxl.load_workbook(plan[f"{kind}_template"],read_only=False,data_only=False)
        target=openpyxl.load_workbook(plan[f"{kind}_output"],read_only=False,data_only=False)
        try:
            for item in samples:
                if item["kind"]!=kind:continue
                a=source[item["sheet"]][item["source"]];b=target[item["sheet"]][item["target"]]
                left,right=signature(a),signature(b);passed=left==right
                results.append({"kind":kind,"sheet":item["sheet"],"source":item["source"],"target":item["target"],"represented_cells":item["represented_cells"],"passed":passed,"before":repr(left),"after":repr(right)})
                if not passed:raise AssertionError(f"Native style mismatch: {kind}/{item['sheet']} {item['source']} -> {item['target']}")
        finally:source.close();target.close()
    save(out/"native-style-checks.json",results)
    hashes={key:hashlib.sha256(Path(plan[key+"_output"]).read_bytes()).hexdigest() for key in ("report","rainbow")}
    save(out/"native-style-hashes.json",hashes)


def action_weekly_preview(plan_path: Path) -> None:
    plan=json.loads(plan_path.read_text(encoding="utf-8-sig"));out=plan_path.parent
    with excel_app() as app:
        rainbow=open_book(app,plan["rainbow_output"],readonly=True)
        try:
            sheet=rainbow.sheets["SGD Promotional Rate"]
            for tenor in ("1M","18M","24M"):
                group=next((g for g in plan["rainbow_groups"] if g["tenor"]==tenor),None)
                if group:
                    col=int(group["col"]);bottom=int(group["last_row"])
                    export_preview(sheet,f"{sheet.range((2,col)).address.split('!')[-1]}:{sheet.range((bottom,col+2)).address.split('!')[-1]}",out/('rainbow-'+tenor.lower()+'.pdf'),save_source=False)
        finally:rainbow.close()
        report=open_book(app,plan["report_output"],readonly=True)
        try:
            sheet=report.sheets["SGD促销"]
            for group in (g for g in plan["groups"] if g["bank"]=="Maybank"):
                first=int(group["report_row"]);last=first+len(group["input_rows"])-1
                # Include whole merged bank/rate cells rather than clipping
                # the bottom of a group that has fewer current input rows.
                for row in (first,last):
                    for col in range(1,6):
                        merged=cell(sheet,row,col).merge_area
                        top=int(merged.row);bottom=top+int(merged.rows.count)-1
                        first=min(first,top);last=max(last,bottom)
                export_preview(sheet,f"A{first}:E{last}",out/('quote-maybank-'+group["display_tenor"].lower()+'.pdf'),save_source=False)
        finally:report.close()


def rebuild_deltas(report, plan):
    # Rebuild daily comparison formulas in their existing delta
    # columns. Formula references stay relative to the latest two
    # dates after Excel inserts the new date column.
    for h in plan["histories"]:
        print('Rebuild delta formulas: '+h['sheet'],flush=True)
        sheet=report.sheets[h["sheet"]];bc=int(h["bank_col"]);cc=int(h["current_col"])
        last=int(h["max_row"]);last_col=max(cc+1,used_last(sheet,"column"))
        matrix=sheet.range((1,1),(last,last_col)).value
        delta_col=None;header_row=1
        for ri,values in enumerate(matrix,1):
            for ci,value in enumerate(values,1):
                if value in ("当日增幅","当日最高报价变动"):
                    delta_col,header_row=ci,ri;break
            if delta_col is not None:break
        if delta_col is None:raise ValueError("FX change header missing: "+h["sheet"])
        for insert in h.get('inserts',[]):
            if insert.get('kind')=='tenor':
                row=int(insert['target_row'])+1
                set_value(cell(sheet,row,delta_col),'当日增幅')
                matrix[row-1][delta_col-1]='当日增幅'
        delta_updates={};delta_spans=[]
        if "促销" in h["sheet"]:
            area=sheet.range((header_row+1,delta_col),(last,delta_col));area.api.unmerge();area.clear_contents()
            delta_updates.update({r:matrix[r-1][delta_col-1] for r in range(header_row+1,last+1) if matrix[r-1][delta_col-1] in ('当日增幅','当日最高报价变动')})
            done=set()
            for row in range(header_row+1,last+1):
                label=str(matrix[row-1][bc-1] or "")
                if not label or label in ("金额","全部公开报价","金额要求","日期","银行","Bank") or label.startswith("Tenor:"):continue
                span=cell(sheet,row,bc).merge_area
                start,end=span.row,span.last_cell.row
                if start in done:continue
                done.add(start)
                delta_spans.append((start,end))
                def max_expr(col):
                    from market_rates.quote_selection import caption_is_personal
                    refs=[f"INDEX({r}:{r},1,{col})" for r in range(start,end+1) if caption_is_personal(matrix[r-1][bc])]
                    return f'IF(COUNT({",".join(refs)})>0,MAX({",".join(refs)}),"-")' if refs else '"-"'
                left,right=max_expr(cc),max_expr(cc+1)
                delta_updates[start]=f'=IF(COUNT({left},{right})=2,ROUND(({left})-({right}),8),"-")'
        else:
            for row in range(header_row+1,last+1):
                values=matrix[row-1];label=values[bc] if bc<len(values) else None
                current=values[cc-1] if cc-1<len(values) else None;previous=values[cc] if cc<len(values) else None
                if not label or label in ("金额","全部公开报价","金额要求","日期","银行","Bank"):continue
                if not any(isinstance(v,(int,float)) for v in (current,previous)):continue
                delta_updates[row]=f'=IF(COUNT(INDEX({row}:{row},1,{cc}),INDEX({row}:{row},1,{cc+1}))=2,ROUND(INDEX({row}:{row},1,{cc})-INDEX({row}:{row},1,{cc+1}),8),"-")'
        write_sparse_formulas(sheet,delta_col,delta_updates)
        for start,end in delta_spans:
            if end>start:merge(sheet,start,delta_col,end,delta_col)
    # Restore the unchanged USD/CNY board comparison columns as well.
    for name,bc in (("USD挂牌",2),("CNY挂牌",1)):
        sheet=report.sheets[name];cc=bc+2;last=used_last(sheet)
        last_col=used_last(sheet,"column");matrix=sheet.range((1,1),(last,last_col)).value
        found=next(((ri,ci) for ri,rowvals in enumerate(matrix,1) for ci,v in enumerate(rowvals,1) if v in ("当日增幅","当日最高报价变动")),None)
        if not found:raise ValueError("Comparison header missing: "+name)
        header_row,delta_col=found
        delta_updates={}
        for row in range(header_row+1,last+1):
            label=matrix[row-1][bc] if bc<len(matrix[row-1]) else None
            now=matrix[row-1][cc-1] if cc-1<len(matrix[row-1]) else None
            previous=matrix[row-1][cc] if cc<len(matrix[row-1]) else None
            bank=matrix[row-1][bc-1] if bc-1<len(matrix[row-1]) else None
            if not label or label in ("金额","全部公开报价","金额要求","日期","银行","Bank") or str(bank or "").startswith("Tenor:"):continue
            if now!="-" and not isinstance(now,(int,float)) and not isinstance(previous,(int,float)):continue
            delta_updates[row]=f'=IF(COUNT(INDEX({row}:{row},1,{cc}),INDEX({row}:{row},1,{cc+1}))=2,ROUND(INDEX({row}:{row},1,{cc})-INDEX({row}:{row},1,{cc+1}),8),"-")'
        write_sparse_formulas(sheet,delta_col,delta_updates)
    # The general daily deltas exclude the special SGD target comparison.
    sgd=report.sheets["SGD促销"];sgd_max=used_last(sgd);sgd_matrix=sgd.range((1,1),(sgd_max,used_last(sgd,"column"))).value
    sgd_header=next(((ri,ci) for ri,rowvals in enumerate(sgd_matrix,1) for ci,v in enumerate(rowvals,1) if v=="当日最高报价变动"),None)
    if not sgd_header:raise ValueError("SGD comparison header missing")
    sgd_header_row,sgd_delta_col=sgd_header;sgd_results=[]
    for row in range(1,sgd_max+1):
        bank=sgd_matrix[row-1][0] if sgd_matrix[row-1] else None
        if bank not in ("UOB","Singapura Finance"):continue
        group=cell(sgd,row,1).merge_area;end=row+int(group.rows.count)-1;previous=4;found=False
        for candidate in range(4,sgd_delta_col):
            if any(isinstance(sgd_matrix[r-1][candidate-1],(int,float)) for r in range(row,end+1)):
                previous=candidate;found=True;break
        target=sgd.range((row,sgd_delta_col),(end,sgd_delta_col));target.api.unmerge();target.clear_contents()
        if end>row:target.api.merge()
        current_ref=sgd.range((row,3),(end,3)).address.replace("$","")
        prior_ref=sgd.range((row,previous),(end,previous)).address.replace("$","")
        set_formula(cell(sgd,row,sgd_delta_col),f'=IF(COUNT({current_ref})=0,"-",IF(COUNT({prior_ref})=0,"无上期数据",ROUND(MAX({current_ref})-MAX({prior_ref}),8)))')
        cell(sgd,row,sgd_delta_col).number_format="0.00%"
        sgd_results.append({"bank":bank,"row":row,"end":end,"delta_column":sgd_delta_col,"previous_column":previous,"has_history":found})
    return sgd_results


def checkpoint_fx_report(plan_path: Path) -> None:
    """Freeze a report checkpoint only after its saved quotes and comparisons pass."""
    from market_rates.common import load,save
    from market_rates.xlsx_read import read_xlsx
    from market_rates.table_validation import sha
    from market_rates.board_plan import col,cell_number
    from scripts.check_board_cleanup import check_deltas
    from market_rates.sgd_comparison import check_saved
    plan=load(plan_path);path=Path(plan['report_output']);values=read_xlsx(path,merge_anchors_only=True)
    formulas=read_xlsx(path,formulas=True,merge_anchors_only=True)
    checks=0
    def verify(name,address,expected):
        nonlocal checks
        check_value(cell_number(values[name].get(address)),expected,name+' '+address);checks+=1
    for item in plan['summary']:verify('最高报价汇总 ',item['address'],item['expected'])
    for item in plan.get('coverage',[]):verify('Bank List',item['address'],item['expected'])
    for history in plan['histories']:
        for item in history['existing']:verify(history['sheet'],item['address'],item['expected'])
        for insert in history['inserts']:
            offset=2 if insert.get('kind')=='tenor' else 0
            for index,item in enumerate(insert['rows']):
                verify(history['sheet'],col(history['current_col'])+str(insert['target_row']+index+offset),item['expected'])
    expected_formulas=[(history['sheet'],item) for history in plan['histories'] for item in history['existing']]
    expected_formulas.extend(('最高报价汇总 ',item) for item in plan['summary'])
    expected_formulas.extend(('Bank List',item) for item in plan.get('coverage',[]))
    for name,item in expected_formulas:
        expected=item.get('formula','')
        if isinstance(expected,str) and expected.startswith('='):
            actual=formulas[name].get(item['address']) or ''
            if actual.replace("'",'')!=expected.replace("'",''):
                raise AssertionError('Saved formula does not reference the current input: '+name+' '+item['address'])
    deltas=check_deltas(path,values);sgd=check_saved(path)
    save(plan_path.parent/'report-native-checkpoint.json',dict(passed=True,checks=checks,
        delta_rows=deltas,sgd=sgd,plan_sha256=sha(plan_path),sha256=sha(path)))


def upgrade_fx_captions(plan_path):
    from market_rates.common import load,save
    from market_rates.report_xml import Book,pos
    from market_rates.fx_promo_plan import unmatched_caption,rank_public_groups
    plan=load(plan_path);original=Book(plan['report_template']);changed=False
    for h in plan['histories']:
        last=max([h['max_row']]+[i['target_row']+i['count']-1 for i in h.get('inserts',[])])
        if last!=h['max_row']:h['max_row']=last;changed=True
        for item in h.get('existing',[]):
            if not str(item.get('amount','')).startswith('本期未列此旧产品或档位'):continue
            prior=original.sheets[h['sheet']].value(item['old_row'],h['bank_col']+1)
            label=unmatched_caption(prior)
            if item['amount']!=label:item['amount']=label;changed=True
    for block in plan.get('rainbow_blocks',[]):
        before=json.dumps(block,sort_keys=True,ensure_ascii=False)
        rank_public_groups(block['groups'],[r for r in plan['details'] if r['insertable']],block['currency'],block['tenor'])
        row=block['start']
        for group in block['groups']:group['target_row']=row;row+=len(group['rows'])
        changed=changed or before!=json.dumps(block,sort_keys=True,ensure_ascii=False)
    if changed:
        import shutil
        backup=plan_path.with_name('before-caption-delta-'+uuid.uuid4().hex+'.json');shutil.copy2(plan_path,backup);save(plan_path,plan)
    return plan


def repair_fx_bank_spans(book,plan):
    from market_rates.report_xml import pos
    for h in plan['histories']:
        sheet=book.sheets[h['sheet']];bc=h['bank_col']
        for item in h.get('existing',[]):
            if not item.get('amount') or '原专属报价仅保留历史' in item.get('amount',''):
                row,_=pos(item['address']);set_value(cell(sheet,row,bc+1),item['amount'])
        for span in h.get('bank_merges',[]):
            merge(sheet,span['start'],bc,span['end'],bc);cell(sheet,span['start'],bc).value=span['bank']
        for insert in h.get('inserts',[]):
            if insert.get('kind')!='tenor':continue
            rows=insert['rows'];offset=0
            while offset<len(rows):
                end=offset+1
                while end<len(rows) and rows[end]['bank']==rows[offset]['bank']:end+=1
                first=insert['target_row']+2+offset;last=insert['target_row']+1+end
                merge(sheet,first,bc,last,bc);cell(sheet,first,bc).value=rows[offset]['bank'];offset=end


def action_rebuild_deltas(plan_path: Path) -> None:
    from market_rates.common import load,save
    from market_rates.xlsx_read import read_xlsx
    from scripts.check_board_cleanup import check_deltas
    from market_rates.sgd_comparison import check_saved
    plan=upgrade_fx_captions(plan_path);path=Path(plan['report_output'])
    with excel_app() as app:
        book=open_book(app,path)
        try:
            repair_fx_bank_spans(book,plan);result=rebuild_deltas(book,plan);app.calculate();book.save()
        finally:book.close()
    deltas=check_deltas(path,read_xlsx(path,merge_anchors_only=True))
    sgd=check_saved(path)
    save(plan_path.parent/'delta-native-repair.json',dict(passed=True,delta_rows=deltas,sgd=sgd,groups=result))
    checkpoint_fx_report(plan_path)
    print('Saved native delta checks passed',flush=True)


def action_fx(plan_path: Path, retry: bool = False) -> None:
    """Apply the frozen FX plan to private workbook copies using desktop Excel."""
    from market_rates.common import save
    from market_rates.table_validation import sha
    plan=upgrade_fx_captions(plan_path);out=plan_path.parent
    checkpoint_path=out/'report-native-checkpoint.json'
    checkpoint=json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    completed_report=(retry and checkpoint.get('passed') and Path(plan['report_output']).is_file()
                      and checkpoint.get('sha256')==sha(plan['report_output'])
                      and checkpoint.get('plan_sha256')==sha(plan_path))
    for kind in ("report","rainbow"):
        if sha(plan[kind+"_template"])!=plan[kind+"_sha256"]:raise ValueError("FX template hash changed")
        if Path(plan[kind+"_output"]).exists() and not (kind=='report' and completed_report):raise FileExistsError("FX output exists: "+plan[kind+"_output"])
    checks=[]
    with excel_app() as app:
        inputs=open_book(app,out/"fx-inputs.xlsx",readonly=True)
        if not completed_report:
            report=copied_book(app,plan["report_template"],plan["report_output"])
            try:
                export_preview(report.sheets["USD促销"],"B39:G60",out/"before-usd-promo.pdf")
                inputs.sheets[0].copy(after=report.sheets[-1])
                if report.sheets[-1].name!=plan["input_sheet"]:raise ValueError("FX input worksheet name collision")
                pending=[]
                for h in plan["histories"]:
                    print("FX history: "+h["sheet"],flush=True)
                    sheet=report.sheets[h["sheet"]];bc=int(h["bank_col"]);cc=int(h["current_col"])
                    for snap in h.get("snapshots",[]):set_value(sheet.range(snap["address"]),snap["value"])
                    if h.get("insert_date"):
                        insert_columns(sheet,cc)
                        copy_formats(
                            sheet.range((1, cc + 1), (int(h["old_max_row"]), cc + 1)),
                            sheet.range((1, cc), (int(h["old_max_row"]), cc)),
                        )
                        for row in range(1,int(h["old_max_row"])+1):
                            if not cell(sheet,row,cc).api.merge_cells.get():cell(sheet,row,cc).value="-"
                    for insert in sorted(h.get("inserts",[]),key=lambda x:(int(x["before"]),int(x.get('target_row',x['before']))),reverse=True):
                        before,count,anchor=int(insert["before"]),int(insert["count"]),int(insert["anchor"])
                        insert_rows(sheet,before,count)
                        if insert.get("kind")=="tenor":
                            sheet.range((before,bc),(before+1,cc+2)).font.bold=True
                            cell(sheet,before,bc).value=insert.get("label","")
                            if insert.get('currency_label'):cell(sheet,before,1).value=insert['currency_label']
                            cell(sheet,before+1,bc).value="银行";cell(sheet,before+1,bc+1).value="全部公开报价"
                            set_value(cell(sheet,before+1,cc),datetime.fromisoformat(plan["as_of"]))
                            cell(sheet,before+1,cc).number_format="yyyy-mm-dd"
                            for offset,item in enumerate(insert.get("rows",[])):
                                dest=before+2+offset
                                src=sheet.range((anchor,1),(anchor,used_last(sheet,"column")))
                                dst=sheet.range((dest,1),(dest,used_last(sheet,"column")))
                                copy_formats(src,dst)
                                cell(sheet,dest,bc).value=str(item.get("bank",""));cell(sheet,dest,bc+1).value=str(item.get("amount",""))
                                set_value(cell(sheet,dest,cc),item.get("formula"));cell(sheet,dest,cc).number_format="0.0000%"
                                sheet.api.rows[dest].row_height.set(132)
                            continue
                        for offset,item in enumerate(insert.get("rows",[])):
                            dest=before+offset;last_col=used_last(sheet,"column")
                            copy_formats(sheet.range((anchor,1),(anchor,last_col)),sheet.range((dest,1),(dest,last_col)))
                            cell(sheet,dest,bc+1).value=str(item.get("amount",""));set_value(cell(sheet,dest,cc),item.get("formula"))
                            cell(sheet,dest,cc).number_format="0.0000%";sheet.api.rows[dest].row_height.set(132 if insert.get("kind")=="tenor" else 96)
                        if insert.get("kind")=="tenor":cell(sheet,before,bc).value=insert.get("label","")
                        else:
                            merge_start=before if insert.get("new_bank") else anchor
                            merge(sheet,merge_start,bc,before+count-1,bc);cell(sheet,merge_start,bc).value=insert.get("label","")
                    for row in h.get("date_rows",[]):
                        set_value(cell(sheet,int(row),cc),datetime.fromisoformat(plan["as_of"]));cell(sheet,int(row),cc).number_format="yyyy-mm-dd"
                    for item in h.get("existing",[]):
                        target=sheet.range(item["address"]);set_value(target,item["formula"]);target.number_format="0.0000%"
                        row=target.row
                        if item.get("amount"):
                            cell(sheet,row,bc+1).value=str(item["amount"]);cell(sheet,row,bc+1).wrap_text=True;sheet.api.rows[row].row_height.set(112)
                        pending.append((sheet,target.row,target.column,item["expected"]))
                    for insert in h.get("inserts",[]):
                        if not insert.get("rows"):continue
                        first=int(insert.get("target_row",insert.get("before",1)))+(2 if insert.get('kind')=='tenor' else 0)
                        last=first+len(insert["rows"])-1
                        area=sheet.range((first,bc+1),(last,cc));area.api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
                        for row,item in enumerate(insert["rows"],first):
                            pending.append((sheet,row,cc,item['expected']))
                            label=str(item.get("amount",""));sheet.api.rows[row].row_height.set(max(48,((len(label)+35)//36)*18+12))
                    sheet.api.columns[bc+1].column_width.set(60);sheet.api.columns[cc].column_width.set(18)
                    if h["sheet"]!="其他外币促销利率":cell(sheet,1,bc).value=h.get("title","")
                    for insert in h.get("inserts",[]):
                        if insert.get("kind")=="tenor":
                            first=int(insert.get("target_row",insert.get("before",1)));last=first+int(insert.get("count",len(insert.get("rows",[]))))-1
                            export_preview(sheet,f"{sheet.range((first,bc)).address}:{sheet.range((last,cc+2)).address}",out/(str(insert.get("currency",h.get("currency","fx"))).lower()+"-new-tenor-preview.pdf"))
                summary=report.sheets["最高报价汇总 "]
                print("FX summary",flush=True)
                assign_cells(summary,plan["summary"],number_format="0.00%")
                for item in plan["summary"]:
                    target=summary.range(item["address"])
                    pending.append((summary,target.row,target.column,item["expected"]))
                bank_list=report.sheets["Bank List"]
                for item in plan.get("coverage",[]):
                    target=bank_list.range(item["address"]);set_value(target,item["formula"]);target.number_format="0"
                    pending.append((bank_list,target.row,target.column,item["expected"]))
                copy_formats(summary.range("B73:P73"),summary.range("B75:P75"))
                summary.range("B27").value="美元促销利率市场调研 "+plan["as_of"]
                cell(summary,27,5).value=datetime.fromisoformat(plan["as_of"]);cell(summary,27,5).number_format="yyyy-mm-dd"
                summary.range("I29:I38").wrap_text=True;summary.range("O69:P75").wrap_text=True
                for row in (69,70,71,72,73,74,75):summary.api.rows[row].row_height.set(84)
                summary.range('F74:P74').wrap_text=True
                summary.range("B69:P75").api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
                for item in plan["summary"]:
                    if "公告期" in str(item.get("expected","")):
                        notice=summary.range(item["address"]);notice.wrap_text=True;notice.font.size=10;notice.api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
                        notice.api.rows[1].row_height.set(max(156,float(notice.row_height or 0)))
                copy_formats(summary.range("B37:I37"),summary.range("B38:I38"))
                summary.range("C29:G38").number_format="0.00%";summary.range("H29:I38").wrap_text=True
                summary.range("B29:I38").api.vertical_alignment.set(appscript_kw.vertical_alignment_center);summary.api.rows[38].row_height.set(84)
                summary.range("B27").value=re.sub(r"\s*20\d{2}-\d{2}-\d{2}\s*$","",str(summary.range("B27").value))
                repair_fx_bank_spans(report,plan);sgd_results=rebuild_deltas(report,plan)
                app.calculate()
                for sheet,row,col,expected in pending:test_cell(sheet,row,col,expected)
                print("FX report checks passed; saving",flush=True)
                report.save()
                from market_rates.common import save as save_json
                save_json(out/"delta-rebuild.json",[{"sheet":h["sheet"],"column":next((ci for ci,v in enumerate(report.sheets[h["sheet"]].range((1,1),(1,used_last(report.sheets[h["sheet"]],"column"))).value,1) if v in ("当日增幅","当日最高报价变动")),None)} for h in plan["histories"]])
                save_json(out/"sgd-delta-rebuild.json",sgd_results)
                for h in plan["histories"]:
                    if h["sheet"] not in ("USD促销","CNY促销","其他外币促销利率"):continue
                    if h["sheet"] in ("USD促销","CNY促销","其他外币促销利率"):
                        bc=int(h["bank_col"]);cc=int(h["current_col"])
                        inserts=[i for i in h.get("inserts",[]) if i.get("rows") and i.get("kind")!="tenor"]
                        if inserts:
                            first=min(int(i.get("target_anchor",i.get("target_row",i["before"]))) for i in inserts)
                            last=max(int(i.get("target_row",i["before"]))+len(i["rows"])-1 for i in inserts)
                        else:
                            first=int(h["date_rows"][0]);last=min(first+5,int(h["max_row"]))
                        preview_sheet=report.sheets[h["sheet"]]
                        export_preview(preview_sheet,f"{preview_sheet.range((first,bc)).address}:{preview_sheet.range((last,cc+2)).address}",out/(h["currency"].lower()+"-promo-preview.pdf"))
                export_preview(summary,"B27:I38",out/"fx-summary-preview.pdf")
                export_preview(summary,"B67:P75",out/"other-fx-summary-preview.pdf")
            finally:report.close()

            for h in plan["histories"]:
                saved_report=open_book(app,plan["report_output"])
                try:remove_quote_icons(saved_report,h["sheet"],int(h["current_col"]))
                finally:
                    try:saved_report.close()
                    except Exception:pass

            checkpoint_fx_report(plan_path)
        else:
            print("FX report checkpoint reused",flush=True)

        rainbow=copied_book(app,plan["rainbow_template"],plan["rainbow_output"])
        print("FX rainbow",flush=True)
        try:
            sheet=rainbow.sheets["USD Rate + Other Currency Rates"]
            export_preview(sheet,"I2:K21",out/"before-usd-rainbow.pdf")
            inputs.sheets[0].copy(after=rainbow.sheets[-1])
            if rainbow.sheets[-1].name!=plan['input_sheet']:raise ValueError('FX rainbow input worksheet name collision')
            style=clone_sheet(sheet,rainbow,"_fx_style_temp")
            usd,cny=int(plan.get("usd_delta",0)),int(plan.get("cny_delta",0))
            if cny:insert_rows(sheet,30,cny)
            if usd:insert_rows(sheet,22,usd)
            from market_rates.fx_promo_plan import rainbow_public_regions
            for first,last,width in rainbow_public_regions(plan,used_last(sheet),used_last(sheet,'column')):
                area=sheet.range((first,1),(last,width));area.api.unmerge();area.clear_contents();clear_conditional_formats(area)
            for title in plan.get("extra_titles",[]):
                row=int(title["row"]);sheet.range((row,1)).value=f"{title['currency']} Promo Rate {plan['as_of']}"
                sheet.range((row,1)).font.bold=True
                for col in (1,5,9,13):
                    sample=25 if title["currency"]=="CNY" else 3
                    style.range((sample,col),(sample,col+2)).copy(destination=sheet.range((row+1,col),(row+1,col+2)))
                    cell(sheet,row+1,col).value={1:"1M",5:"3M",9:"6M",13:"12M"}[col]
            pending=[]
            for block in plan["rainbow_blocks"]:
                print('FX rainbow: '+block['currency']+' '+str(block['tenor'])+' ('+str(sum(len(g['rows']) for g in block['groups']))+' quotes)',flush=True)
                col,start,end=int(block["col"]),int(block["start"]),int(block["end"])
                cell(sheet,start-1,col).value=block['tenor']
                if block['tenor'] is not None:
                    cell(sheet,start-1,col+1).value='最高报价';cell(sheet,start-1,col+2).value='金额要求'
                area=sheet.range((start,col),(end,col+2));area.api.unmerge();area.clear_contents();clear_conditional_formats(area)
                for group in block["groups"]:
                    row=int(group["target_row"])
                    for item in group["rows"]:
                        sample=25 if block["currency"]=="CNY" else 4
                        stylecol=col if col<=17 else 1
                        style.range((sample,stylecol),(sample,stylecol+2)).copy(destination=sheet.range((row,col),(row,col+2)))
                        row_area=sheet.range((row,col),(row,col+2));clear_conditional_formats(row_area)
                        cell(sheet,row,col).clear_contents();set_value(cell(sheet,row,col+1),item["formula"])
                        cell(sheet,row,col+1).number_format="0.0000%";cell(sheet,row,col+2).value=str(item["amount"])
                        area=sheet.range((row,col),(row,col+2));area.color=_excel_color(block["color"])
                        area.font.size=10;area.api.vertical_alignment.set(appscript_kw.vertical_alignment_center);area.wrap_text=True
                        needed=min(400,max(84,((len(str(item["amount"]))+19)//20)*17+28))
                        sheet.api.rows[row].row_height.set(max(needed,sheet.range((row,col)).row_height or 0))
                        pending.append((sheet,row,col+1,item["expected"]));row+=1
                    merge(sheet,int(group["target_row"]),col,row-1,col);cell(sheet,int(group["target_row"]),col).value=group["bank"]
            style.delete()
            sheet.range("A1").value=f"外币促销 {plan['as_of']}";sheet.range("A2").value="USD Promo Rate"
            sheet.range((23+usd,1)).value="CNY Promo Rate（含CNH）"
            for item in plan["mixed_cells"]:
                target=sheet.range(item["address"]);set_value(target,item["formula"]);target.number_format="0.0000%"
                pending.append((sheet,target.row,target.column,item["expected"]))
            offset=usd+cny
            sheet.range((106+offset,15),(118+offset,15)).wrap_text=True
            for row in range(106+offset,119+offset):
                sheet.api.rows[row].row_height.set(150)
                sheet.range((row,14),(row,15)).font.size=10;sheet.range((row,14),(row,15)).wrap_text=True
            app.calculate()
            for target_sheet,row,col,expected in pending:test_cell(target_sheet,row,col,expected)
            export_preview(sheet,"I2:K12",out/"usd-rainbow-preview.pdf")
            export_preview(sheet,f"A{23+usd}:G{29+offset}",out/"cny-rainbow-preview.pdf")
            export_preview(sheet,f"A{104+offset}:O{107+offset}",out/"mixed-fx-preview.pdf")
            for title in plan.get("extra_titles",[]):
                row=int(title["row"]);export_preview(sheet,f"A{row}:O{row+3}",out/(title["currency"].lower()+"-extra-rainbow-preview.pdf"))
            rainbow.save()
        finally:rainbow.close();inputs.close()
    save(out/"native-checks.json",[{"name":"FX report and rainbow formula readback","passed":True}])


def action_board_cleanup(plan_path: Path, report_output: Path) -> None:
    """Compact duplicated rows while retaining all observations and formulas."""
    from market_rates.common import save
    from market_rates.table_validation import sha
    from market_rates.board_plan import col
    plan=json.loads(plan_path.read_text(encoding="utf-8-sig"))
    report_output=report_output.resolve();out=Path(plan["output"])
    if sha(plan["report"]) != plan.get("report_sha256", sha(plan["report"])):
        raise ValueError("Board cleanup source hash changed")
    if report_output.exists():raise FileExistsError(f"Refusing to overwrite {report_output}")
    delta_results=[];sgd_results=[]
    with excel_app() as app:
        book=copied_book(app,plan["report"],report_output)
        try:
            # Unmerge bank labels before row deletion shifts the old spans.
            for h in plan["sheets"]:
                sheet=book.sheets[h["sheet"]];bc=int(h["bank_col"])
                for m in h["bank_merges"]:
                    area=cell(sheet,int(m["original_start"]),bc).api.merge_area
                    area.unmerge()
                put_batched(sheet,h["updates"])
                for row in sorted(map(int,h["delete_rows"]),reverse=True):
                    sheet.api.rows[row].delete()
                for m in h["bank_merges"]:
                    merge(sheet,int(m["start"]),bc,int(m["end"]),bc)
                    cell(sheet,int(m["start"]),bc).value=m["label"]
                    sheet.range((int(m["start"]),bc),(int(m["end"]),bc)).api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
                formatted_rows=set()
                for row,first,last in contiguous_update_spans(h["updates"]):
                    sheet.range((row,first),(row,last)).number_format="0.0000%"
                    formatted_rows.add(row)
                for row in formatted_rows:sheet.api.rows[row].row_height.set(36)
                print(f"Board cleanup formatted {h['sheet']}: {len(formatted_rows)} rows",flush=True)

            promo=plan.get("sgd_promo") or {}
            if promo.get("delete_rows"):
                sheet=book.sheets["SGD促销"]
                for m in promo.get("bank_merges",[]):
                    cell(sheet,int(m["start"]),1).api.merge_area.unmerge()
                for item in promo.get("updates",[]):
                    dest=sheet.range(item["address"]);set_value(dest,item["value"]);dest.number_format="0.00%"
                for row in sorted(map(int,promo["delete_rows"]),reverse=True):sheet.api.rows[row].delete()
                for m in promo.get("bank_merges",[]):
                    original_start,original_end=int(m["start"]),int(m["end"])
                    deleted=[int(r) for r in promo["delete_rows"]]
                    start=original_start-sum(r<original_start for r in deleted)
                    end=original_end-sum(r<=original_end for r in deleted)
                    merge(sheet,start,1,end,1);cell(sheet,start,1).value=m["label"]

            if not plan['cimb_sgd'].get('preserve'):
                c=plan["cimb_sgd"];sheet=book.sheets["SGD挂牌"]
                cell(sheet,int(c["start"]),1).api.merge_area.unmerge()
                for item in c["updates"]:set_value(sheet.range(item["address"]),item["value"])
                cell(sheet,int(c["start"]),2).value=c["label"];cell(sheet,int(c["start"]),19).value=c["note"]
                for row in sorted(map(int,c["delete_rows"]),reverse=True):sheet.api.rows[row].delete()
                merge(sheet,int(c["start"]),1,int(c["start"])+1,1);cell(sheet,int(c["start"]),1).value="CIMB"
                sheet.api.rows[int(c["start"])].row_height.set(60)
            for sh in book.sheets:
                if not sh.name.endswith("挂牌") or sh.name=="SGD挂牌":continue
                bc=1 if sh.name=="CNY挂牌" else 2;last=used_last(sh)
                for r in range(1,last+1):
                    bank=cell(sh,r,bc).value or (cell(sh,r-1,bc).value if r>1 else "")
                    label=str(cell(sh,r,bc+1).value or "")
                    if bank=="Maybank" and "外币定存挂牌" in label:
                        sh.range((r,bc+1)).wrap_text=True;sh.range((r,bc+1)).api.vertical_alignment.set(appscript_kw.vertical_alignment_center)
                        sh.api.rows[r].row_height.set(60)
                        sh.range((r,bc+2),(r,used_last(sh,"column"))).api.vertical_alignment.set(appscript_kw.vertical_alignment_center)

            # Restore daily comparison formulas using the final row positions.
            for name in ("USD促销","CNY促销","其他外币促销利率","USD挂牌","CNY挂牌"):
                if name not in [s.name for s in book.sheets]:continue
                sh=book.sheets[name];bc=1 if name.startswith("CNY") else 2;cc=bc+2
                maxr=used_last(sh);matrix=sh.range((1,1),(maxr,used_last(sh,"column"))).value
                header=None;dc=None
                for ri,rowvals in enumerate(matrix,1):
                    for ci,v in enumerate(rowvals,1):
                        if v in ("当日增幅","当日最高报价变动"):header,dc=ri,ci;break
                    if header:break
                if not header:raise ValueError("Comparison header missing: "+name)
                for r in range(header+1,maxr+1):
                    label=matrix[r-1][bc] if bc<len(matrix[r-1]) else None
                    if not label or label in ("金额","全部公开报价","金额要求","日期") or label in ("银行","Bank") or str(label).startswith("Tenor:"):continue
                    cur=matrix[r-1][cc-1] if cc-1<len(matrix[r-1]) else None
                    prev=matrix[r-1][cc] if cc<len(matrix[r-1]) else None
                    if cur!="-" and not isinstance(cur,(float,int)) and not isinstance(prev,(float,int)):continue
                    if "促销" in name:
                        area=cell(sh,r,bc).merge_area;first=int(area.row);end=first+int(area.rows.count)-1
                        if r!=first:continue
                        def expr(c):
                            from market_rates.quote_selection import caption_is_personal
                            refs=[f"INDEX({rr}:{rr},1,{c})" for rr in range(first,end+1) if caption_is_personal(matrix[rr-1][bc])]
                            return f'IF(COUNT({",".join(refs)})>0,MAX({",".join(refs)}),"-")' if refs else '"-"'
                        left,right=expr(cc),expr(cc+1)
                        dest=sh.range((first,dc),(end,dc));dest.api.unmerge();dest.clear_contents()
                        if end>first:dest.api.merge()
                        target=cell(sh,first,dc);set_formula(target,f'=IF(COUNT({left},{right})=2,ROUND(({left})-({right}),8),"-")')
                    else:
                        set_formula(cell(sh,r,dc),f'=IF(COUNT(INDEX({r}:{r},1,{cc}),INDEX({r}:{r},1,{cc+1}))=2,ROUND(INDEX({r}:{r},1,{cc})-INDEX({r}:{r},1,{cc+1}),8),"-")')
                    cell(sh,r if "促销" not in name else first,dc).number_format="0.00%"
                    delta_results.append({"sheet":name,"row":r if "促销" not in name else first,"column":dc})

            sh=book.sheets["SGD促销"];header_row=None;dc=None
            for ri,rowvals in enumerate(sh.used_range.value,1):
                for ci,v in enumerate(rowvals,1):
                    if v=="当日最高报价变动":header_row,dc=ri,ci;break
                if header_row:break
            if not header_row:raise ValueError("SGD comparison header missing")
            maxr=used_last(sh);data=sh.range((1,1),(maxr,dc-1)).value
            for r in range(1,maxr+1):
                if data[r-1][0] not in ("UOB","Singapura Finance"):continue
                area=cell(sh,r,1).merge_area;end=r+int(area.rows.count)-1;previous=4
                for cc in range(4,dc):
                    if any(isinstance(data[rr-1][cc-1],(int,float)) for rr in range(r,end+1)):
                        previous=cc;break
                target=sh.range((r,dc),(end,dc));target.api.unmerge();target.clear_contents()
                if end>r:target.api.merge()
                now=sh.range((r,3),(end,3)).address.replace("$","");prior=sh.range((r,previous),(end,previous)).address.replace("$","")
                set_formula(cell(sh,r,dc),f'=IF(COUNT({now})=0,"-",IF(COUNT({prior})=0,"无上期数据",ROUND(MAX({now})-MAX({prior}),8)))')
                cell(sh,r,dc).number_format="0.00%";sgd_results.append({"bank":data[r-1][0],"row":r,"end":end,"previous_column":previous})
            app.calculation="automatic";app.calculate()
            book.save()
        finally:book.close()
    save(out/"delta-rebuild.json",delta_results);save(out/"sgd-delta-rebuild.json",sgd_results)


def group_description(detail, bank_dates, manual_note=None):
    short={"rhb-sgd-promo":"定存促销","cimb-sgd-online":"普通在线","cimb-wwfd-online":"Why Wait",
           "cimb-preferred-welcome":"Preferred迎新","hlf-branch-promo":"普通促销","hlf-digital-promo":"定存促销",
           "boc-sgd-welcome":"新客户促销","singapura-sgd-online":"Vivid在线定存","maybank-sgd-bundle":"存款组合促销"}.get(detail.get("product_id"),"定存促销")
    short=detail.get('product_name') or short
    channels={"online":"在线","branch":"分行","branch_or_online":"分行/在线","online_banking|sc_mobile":"网银/SC Mobile",
              "branch_or_mobile":"分行/手机","branch_or_instruction":"分行/指令表单","staff_instruction":"分行/客户经理指令","hlf_digital":"HLF Digital"}
    audience={"premier_elite":"Premier Elite","premier_wealth":"Premier(财富/FX)","premier_standard":"Premier(无财富持仓)"}.get(detail.get("audience"),detail.get("audience","待核"))
    minimum=detail.get("amount_min");maximum=detail.get("amount_max")
    amount="金额待核" if minimum is None else "S$"+format(Decimal(str(minimum)),",.2f").rstrip("0").rstrip(".")
    if maximum is not None:amount+=" - "+("" if detail.get("max_inclusive") else "<")+format(Decimal(str(maximum)),",.2f").rstrip("0").rstrip(".")
    else:amount+="起"
    extra=";新资金" if detail.get("fresh_funds")=="yes" else ""
    if detail.get("rate_basis")=="unknown":extra+=";计息口径待核"
    if detail.get("bank")=="HLF" and detail.get("tenor_value")==8:extra+=";实际8个月"
    if detail.get("bank")=="CITI":extra+=";客户资格见明细"
    if detail.get("product_id")=="singapura-sgd-online":extra+=";须有Vivid储蓄账户"
    if detail.get("product_id")=="singapura-sgd-promo":extra+=";须有储蓄账户(新开S$200)"
    if detail.get("reference_quote") or (detail.get("bank")=="BOC" and detail.get("valid_from") and detail.get("valid_to")):
        extra+=f"\n公告期 {detail.get('valid_from')}–{detail.get('valid_to')}"
    if detail.get("product_id")=="maybank-sgd-bundle":
        terms=detail.get("conditions","")
        for pattern, replacement in ((r"组合有效年利率：([\d.]+)%",r"\n组合有效年利率\1%"),(r"附加存款比例：([\d.]+)%",r";须另存\1%并锁定")):
            terms=re.sub(pattern,replacement,terms)
        extra+=terms
    note=f"\n{manual_note}" if manual_note else ""
    rate=Decimal(str(detail.get("rate_pct",0)))
    return f"{short} / {audience} / {rate:.2f}%\n{detail.get('tenor_value')}{detail.get('tenor_unit')}; {amount}; {channels.get(detail.get('channel'),detail.get('channel','渠道待核'))}{extra}{note}"


def rate_formula(input_sheet: str, rows) -> str:
    if not rows:return '="-"'
    return "=MAX("+",".join(f"'{input_sheet}'!H{int(row)}" for row in rows)+")"


def remove_quote_icons(book, sheet_name: str, column: int = 3) -> None:
    """Drop icon-set rules that cover this quote column, leaving history rules intact."""
    import zipfile
    import xml.etree.ElementTree as ET
    import re
    from market_rates.report_xml import M, R, serialize, remove_column_icons
    path=Path(book.fullname).resolve();temp=path.with_suffix(".icons.tmp.xlsx")
    # Finish Excel's save before reading the ZIP; never replace a workbook
    # while it remains open in Excel.
    book.save();book.close()
    with zipfile.ZipFile(path,"r") as src,zipfile.ZipFile(temp,"w",zipfile.ZIP_DEFLATED) as dst:
        wb=ET.fromstring(src.read("xl/workbook.xml"));rels=ET.fromstring(src.read("xl/_rels/workbook.xml.rels"))
        relmap={r.get("Id"):r.get("Target") for r in rels};sheetpath=None
        for sh in wb.findall(M+"sheets/"+M+"sheet"):
            if sh.get("name")==sheet_name:
                relid=sh.get(R+"id");target=relmap[relid];sheetpath=target.lstrip("/") if target.startswith("/") else "xl/"+target
                break
        if not sheetpath:raise KeyError(sheet_name)
        for info in src.infolist():
            data=src.read(info.filename)
            if info.filename==sheetpath:
                root=ET.fromstring(data)
                remove_column_icons(root,column)
                # Ignorable/extension records refer to namespace prefixes as
                # text. Plain ET.tostring drops their declarations and makes
                # an otherwise valid workbook require Excel content repair.
                data=serialize(root,data)
            dst.writestr(info,data)
    os.replace(temp,path)


def action_sgd(plan_path: Path, retry: bool = False) -> None:
    from market_rates.common import save
    from market_rates.table_validation import sha
    plan=json.loads(plan_path.read_text(encoding="utf-8-sig"));out=plan_path.parent
    if plan.get("mode")!="validation_only":raise ValueError("Only validation workbooks may be generated")
    receipt_path=out/'sgd-native-checkpoint.json'
    receipt=json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    if retry and receipt.get('plan_sha256')==sha(plan_path) and all(Path(plan[k+'_output']).is_file() and receipt.get(k)==sha(plan[k+'_output']) for k in ('report','rainbow')):
        return
    for kind in ("report","rainbow"):
        if sha(plan[kind+"_template"])!=plan[kind+"_sha256"]:raise ValueError("Template hash changed before Excel generation")
        target=Path(plan[kind+"_output"])
        if target.exists():
            if not retry:raise FileExistsError(f"Mac validation output exists: {target}; use a fresh run directory")
            target.resolve().relative_to(out.resolve())
            archived=out/('incomplete-native-'+datetime.now().strftime('%Y%m%d-%H%M%S%f'));archived.mkdir()
            target.rename(archived/target.name)
    details={int(d["input_row"]):d for d in plan["details"]}
    from market_rates.quote_selection import personal_rows
    from market_rates.quote_layout import prepare_sgd_layout
    layout_source=None
    if plan.get('new_sections'):
        layout_source=out/('sgd-layout-'+uuid.uuid4().hex+'.xlsx')
        prepare_sgd_layout(plan['report_template'],plan['new_sections'],layout_source)
    checks=[]
    with excel_app() as app:
        source=open_book(app,out/"validation-inputs.xlsx",readonly=True)
        report=copied_book(app,layout_source or plan["report_template"],plan["report_output"])
        if layout_source:layout_source.unlink()
        try:
            sheet=report.sheets["SGD促销"]
            if int(plan.get("date_column_shift",1)):
                old_max_row=used_last(sheet,"row")
                insert_columns(sheet,3)
                for first,last in inserted_column_copy_segments(Path(plan["report_template"]),"SGD促销",3,old_max_row):
                    copy_formats(sheet.range((first,4),(last,4)),sheet.range((first,3),(last,3)))
            for addr in plan["current_cells"]:
                r=int(re.sub(r"[A-Z]","",addr));dest=cell(sheet,r,3)
                try:
                    area=dest.merge_area
                    if area.column==3:area.value="-"
                except Exception:dest.value="-"
            for row in plan["date_rows"]:
                target=cell(sheet,int(row),3)
                set_formula(target,"=$C$3" if int(row)!=3 else None)
                if int(row)==3:target.value=datetime.fromisoformat(plan["as_of"])
                target.number_format="yyyy-mm-dd"
            # A reference row supplies the complete border, font and alignment for new quotes.
            style_row=int(plan.get("style_row") or 54)
            style=sheet.range((style_row,1),(style_row,3))
            for item in sorted(plan["report_inserts"],key=lambda x:(int(x["at"]),int(x['target_start'])),reverse=True):
                at,count=int(item["at"]),int(item["count"])
                insert_rows(sheet,at,count)
                for i in range(count):
                    style.copy(destination=sheet.range((at+i,1),(at+i,3)))
            source.sheets[0].copy(after=report.sheets[-1])
            if report.sheets[-1].name!=plan["input_sheet"]:raise ValueError("Input sheet name collision")
            for update in plan["report_updates"]:
                group=update.get("group");start=int(update["target_start"]);height=int(update["target_end"])-start+1
                if group:
                    bankcell=cell(sheet,start,1);prior=cell(sheet,start,4).value
                    merge(sheet,start,3,start+height-1,3).value="-"
                    # Current quote captions are rebuilt completely. Old tiers
                    # remain in historical date columns, never in current B.
                    sheet.range((start,2),(start+height-1,2)).api.unmerge()
                    sheet.range((start,2),(start+height-1,2)).clear_contents()
                    n=len(group["details"]);main_rows=personal_rows(group['details'])
                    if n>1:merge(sheet,start,3,start+n-1,3)
                    rate=cell(sheet,start,3);set_formula(rate,rate_formula(plan["input_sheet"],main_rows));rate.number_format="0.00%";rate.font.api.strikethrough.set(False)
                    for j,d in enumerate(group["details"]):
                        note=group_description(d,plan.get("bank_dates",{}),plan.get("manual_notes",{}).get(group["id"]))
                        desc=cell(sheet,start+j,2);desc.value=note;desc.wrap_text=True;desc.font.size=11;desc.font.api.strikethrough.set(False)
                        sheet.api.rows[start+j].row_height.set(90 if (d.get("reference_quote") or d.get("bank")=="BOC" or plan.get("manual_notes",{}).get(group["id"])) else 42)
                    status=("已人工确认；沿用已核验采集结果" if group.get("human_reviewed") else
                            "新增独立产品行（待复核）" if update["start"]==0 else
                            "本周已检查，数字未调整（待复核）" if group['main_pct'] is not None and isinstance(prior,(int,float)) and abs(float(prior)-float(group["main_pct"])/100)<1e-10 else "本周已检查，数字调整（待复核）")
                    cell(sheet,start,2).value=(cell(sheet,start,2).value or "")+f"\n{status}；证据 {plan.get('bank_dates',{}).get(group['bank'],'待核')}"
                    if group.get("bank")=="Maybank":sheet.api.rows[start].row_height.set(80)
                    if update.get("new_bank"):
                        if n>1:merge(sheet,start,1,start+n-1,1)
                        bankcell.value=group["bank"]
                elif update.get("missing_status"):
                    cell(sheet,start,2).value=update["missing_status"]+"；本期不参与报价比较";cell(sheet,start,2).wrap_text=True
                else:
                    sheet.range((start,2),(start+height-1,2)).api.unmerge()
                    sheet.range((start,2),(start+height-1,2)).clear_contents()
                    cell(sheet,start,2).value='本期无已核验报价'
            for insert in plan["report_inserts"]:
                if not insert.get("extension"):
                    group=insert["group"];start=int(insert["target_start"]);n=len(group["details"])
                    rows=personal_rows(group['details'])
                    merge(sheet,start,3,start+n-1,3);set_formula(cell(sheet,start,3),rate_formula(plan["input_sheet"],rows));cell(sheet,start,3).number_format="0.00%"
                    if insert.get("new_bank"):
                        merge(sheet,start,1,start+n-1,1);cell(sheet,start,1).value=group["bank"]
                    for j,d in enumerate(group["details"]):cell(sheet,start+j,2).value=group_description(d,plan.get("bank_dates",{}))
            for item in plan.get("bank_merges",[]):
                merge(sheet,int(item["start"]),1,int(item["end"]),1);cell(sheet,int(item["start"]),1).value=item["bank"]
            for item in plan["deltas"]:set_formula(cell(sheet,int(item["row"]),int(item["col"])),item["formula"])
            sheet.range("A1").value=f"{plan['as_of']} {plan['collection_label']}（待复核；各行注明证据日；其余本期为-）"
            summary=report.sheets["最高报价汇总 "];summary.range("B2").value="新元促销（"+"/".join(plan["banks"])+"；证据日见明细；其余保留底稿）"
            for item in plan.get('summary_headers',[]):cell(summary,item['row'],item['col']).value=item['tenor']
            for item in plan.get('summary_metadata',[]):
                target=cell(summary,item['row'],item['col']);set_value(target,item['value']);target.wrap_text=True
                summary.api.rows[item['row']].row_height.set(min(409,max(float(target.row_height or 0),32*len(item['value'].splitlines()))))
            for item in plan["summary"]:
                target=cell(summary,int(item["row"]),int(item["col"]))
                if item["input_rows"]:set_formula(target,rate_formula(plan["input_sheet"],item["input_rows"]));target.number_format="0.00%"
                else:target.value="-"
            app.calculate()
            for group in plan["groups"]:
                target=cell(sheet,int(group["report_row"]),3)
                check_value(target.value,float(group["main_pct"])/100 if group['main_pct'] is not None else '-',"SGD main quote "+group["id"])
            for item in plan["summary"]:
                if item["input_rows"]:
                    highest=max(float(details[row]["rate_pct"])/100 for row in item["input_rows"])
                    check_value(cell(summary,int(item["row"]),int(item["col"])).value,highest,"SGD maximum "+item["bank"]+"/"+item["tenor"])
            report.save();report.close()
            saved_report=open_book(app,plan["report_output"])
            remove_quote_icons(saved_report,"SGD促销",3)
            rb=copied_book(app,plan["rainbow_template"],plan["rainbow_output"])
            try:
                rb.sheets[0].delete() if False else None
                source.sheets[0].copy(after=rb.sheets[-1])
                if rb.sheets[-1].name!=plan["input_sheet"]:raise ValueError("Input sheet name collision")
                group_lookup={item["id"]:item for item in plan["groups"]}
                target_sheet=rb.sheets["SGD Promotional Rate"]
                old_last_row=used_last(target_sheet,"row")
                for rg in plan["rainbow_groups"]:
                    col=int(rg["col"]);right=col+2;end=max(int(rg["last_row"]),old_last_row)
                    if col>21:copy_formats(target_sheet.range('A2:C2'),target_sheet.range((2,col),(2,right)))
                    for offset,value in enumerate((rg['tenor'],'Promo Rate','Condition')):cell(target_sheet,2,col+offset).value=value
                    block_area=target_sheet.range((3,col),(end,right));block_area.api.unmerge();block_area.clear_contents()
                    for block in rg["blocks"]:
                        start=int(block["target_start"]);rows=block["rows"]
                        if block.get("group"):
                            group_value=block["group"]
                            group=group_lookup[group_value] if isinstance(group_value,str) else group_value
                            for j,d in enumerate(group["details"]):
                                target=cell(target_sheet,start+j,col+1);set_formula(target,f"='{plan['input_sheet']}'!H{d['input_row']}");target.number_format="0.00%"
                                cell(target_sheet,start+j,col+2).value=group_description(d,plan.get("bank_dates",{}))
                                target_sheet.api.rows[start+j].row_height.set(72 if (d.get("reference_quote") or d.get("bank")=="BOC") else 42)
                        else:
                            for j,row in enumerate(rows):
                                if block.get("source_start"):
                                    target_sheet.range((int(block["source_start"])+j,col),(int(block["source_start"])+j,right)).copy(destination=target_sheet.range((start+j,col),(start+j,right)))
                                cell(target_sheet,start+j,col+1).value=row[1];cell(target_sheet,start+j,col+2).value=row[2]
                        merge(target_sheet,start,col,start+len(rows)-1,col)
                        cell(target_sheet,start,col).value=block["bank"]
                    for label in rg.get("bank_labels",[]):
                        merge(target_sheet,int(label["start"]),col,int(label["end"]),col)
                        cell(target_sheet,int(label["start"]),col).value=label["bank"]
                style_new_sgd_rainbow_groups(target_sheet,plan['rainbow_groups'])
                restore_sgd_rainbow_rate_fills(target_sheet,plan['rainbow_groups'],plan['rainbow_template'])
                rb.sheets["SGD Promotional Rate"].range("A1").value=f"{plan['as_of']} {plan['collection_label']}（待复核；各银行证据日见明细；其余保留底稿；改值后重新生成排序）"
                app.calculate()
                for rg in plan["rainbow_groups"]:
                    for block in rg["blocks"]:
                        group_id=block.get("group")
                        if group_id:
                            group=group_id if isinstance(group_id,dict) else group_lookup[group_id]
                            group_label=group.get("id",group.get("bank","unknown"))
                            for offset,detail in enumerate(group["details"]):
                                check_value(cell(rb.sheets["SGD Promotional Rate"],int(block["target_start"])+offset,int(rg["col"])+1).value,
                                            float(detail["rate_pct"])/100,"Rainbow quote "+str(group_label))
                rb.save();rb.close()
            finally:
                pass
            checks.append({"name":"report_and_rainbow_formula_recalculation","passed":True})
        finally:source.close()
    save(out/"native-checks.json",checks)
    save(receipt_path,dict(plan_sha256=sha(plan_path),**{k:sha(plan[k+'_output']) for k in ('report','rainbow')}))


def action_excel_smoke(result: Path) -> None:
    """Exercise a disposable existing file, using the workflow's native open.

    Creating an unsaved Excel book and using Save As has different sandbox
    and path restrictions on Mac. A minimal seed avoids that fragile route.
    """
    from openpyxl import Workbook
    result = result.resolve()
    result.parent.mkdir(parents=True, exist_ok=True)
    source = result.parent / ('codex-smoke-' + uuid.uuid4().hex + '.xlsx')
    seed = Workbook()
    try:
        seed.save(source)
    finally:
        seed.close()
    try:
        with excel_app() as app:
            book = None
            try:
                book = open_book(app, source)
                sheet = book.sheets[0]
                sheet.range('A1').value = 2
                sheet.range('A2').value = 3
                set_formula(sheet.range('A3'), '=SUM(A1:A2)')
                app.calculate()
                check_value(sheet.range('A3').value, 5, 'Excel calculation')
                sheet.range('B1:B2').insert(shift='down')
                book.save()
                export_preview(sheet, 'A1:C3', result)
                if not result.is_file():
                    raise AssertionError('Excel PDF preview was not created')
            finally:
                if book is not None:
                    try: book.close()
                    except Exception: pass  # retain the original failure
    finally:
        source.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("board", "board-batch", "board-single", "board-repair", "board-cleanup", "sgd", "fx", "rebuild-deltas", "scrub-sgd", "check-delivery", "check-premier", "style-check", "weekly-preview", "excel-smoke"))
    parser.add_argument("plan", nargs="?")
    parser.add_argument("-PlanPath", "--PlanPath", dest="plan_path")
    parser.add_argument("-Retry", "--Retry", dest="retry", action="store_true")
    parser.add_argument("--Manifest")
    parser.add_argument("--Out")
    parser.add_argument("-ReportOutput", "--ReportOutput", dest="report_output")
    args = parser.parse_args(argv)
    if args.operation in ("board","board-batch","board-single"):
        action_board(Path(args.plan_path or args.plan), args.operation)
    elif args.operation=="board-repair":action_board_repair(Path(args.plan_path or args.plan))
    elif args.operation=="fx":action_fx(Path(args.plan_path or args.plan),retry=args.retry)
    elif args.operation=="rebuild-deltas":action_rebuild_deltas(Path(args.plan_path or args.plan))
    elif args.operation=="board-cleanup":action_board_cleanup(Path(args.plan_path or args.plan),Path(args.report_output))
    elif args.operation=="sgd":action_sgd(Path(args.plan_path or args.plan),retry=args.retry)
    elif args.operation=="style-check":action_native_styles(Path(args.plan_path or args.plan))
    elif args.operation=="weekly-preview":action_weekly_preview(Path(args.plan_path or args.plan))
    elif args.operation == "scrub-sgd":action_scrub_sgd(Path(args.Manifest))
    elif args.operation == "check-delivery":
        action_check_delivery(Path(args.Manifest), Path(args.Out))
    elif args.operation == "check-premier":
        action_premier_checks(Path(args.Manifest),Path(args.Out))
    elif args.operation == "excel-smoke":
        action_excel_smoke(Path(args.plan or 'data/excel-smoke.pdf'))
    else:
        raise NotImplementedError(f"{args.operation} writer is not implemented yet")
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"Excel operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

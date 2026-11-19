"""Strict table coordinates and column evidence for local weight profiles."""

import re
from decimal import Decimal
from urllib.parse import quote

from .pdf_profile import PDF_READERS

_TABLE = r"(?:/parts/\d{1,6}/document)?/tables/\d{1,6}"
_CELL = re.compile(rf"(?P<table>{_TABLE})/data/table_cells/\d+/text")
_UNIT = r"kg|kgs|g|lb|lbs|千克|公斤|克|磅"
_UNIT_FAMILY = {"kgs": "kg", "千克": "kg", "公斤": "kg", "克": "g", "lbs": "lb", "磅": "lb"}
_KIND = {
    "net_weight": r"net\s+(?:weight|wt\.?)|n\.?\s*w\.?|净重",
    "gross_weight": r"gross\s+(?:weight|wt\.?)|g\.?\s*w\.?|毛重",
    "chargeable_weight": r"chargeable\s+(?:weight|wt\.?)|c\.?\s*w\.?|计费重(?:量)?",
}
_ITEM = r"unit\s+|per[ -](?:item|unit|carton|box)\s+|每(?:件|箱)|单(?:件|箱)"
_ITEM_SUFFIX = r"\s*(?:per\s+|/\s*)(?:item|unit|carton|box)|\s*/\s*(?:件|箱)"
_LINE = r"(?:line(?:\s+total)?|row(?:\s+total)?|total)\s+|整行|行合计"
_MARKER = re.compile(
    r"(?:grand\s+total|shipment\s+total|sub\s*total|total|整票总计|总计|合计|小计)\s*[:：]?", re.I
)
_SUMMARY_PREFIX = re.compile(
    r"^(?:grand\s+total|shipment\s+total|sub\s*total|total)(?:\b|[:：])|^(?:整票总计|总计|合计|小计)",
    re.I,
)
_GRAND = re.compile(r"(?:grand\s+total|shipment\s+total|整票总计)\s*[:：]?", re.I)
_VALUE = re.compile(rf"\s*(\d+(?:\.\d+)?)\s*({_UNIT})?\s*", re.I)
_COORDS = (
    "start_row_offset_idx",
    "end_row_offset_idx",
    "start_col_offset_idx",
    "end_col_offset_idx",
)


def table_weight_object(material_id, selector):
    """Validate a caller-selected table and return its stable local object ID."""
    if not isinstance(selector, dict):
        raise ValueError("weight_table must be a table selector")
    location = {"page"} if "page" in selector else {"worksheet"}
    is_row = "row" in selector
    keys = {"table", *location, *({"row", "row_label"} if is_row else set())}
    if (
        set(selector) != keys
        or not isinstance(selector["table"], str)
        or not re.fullmatch(_TABLE, selector["table"])
    ):
        raise ValueError("weight_table must name one READ table and one page or worksheet")
    if "page" in selector:
        if type(selector["page"]) is not int or selector["page"] < 1:
            raise ValueError("weight_table page must be a positive integer")
        place = f"page-{selector['page']}"
    else:
        sheet = selector["worksheet"]
        if (
            not isinstance(sheet, str)
            or not sheet.strip()
            or len(sheet) > 31
            or "\r" in sheet
            or "\n" in sheet
            or selector["table"].startswith("/parts/")
        ):
            raise ValueError("weight_table worksheet must name one Excel sheet")
        place = "worksheet-" + quote(sheet, safe="")
    if not is_row:
        return material_id + "/document"
    label = selector["row_label"]
    if (
        type(selector["row"]) is not int
        or selector["row"] < 1
        or not isinstance(label, str)
        or not label.strip()
        or len(label) > 120
        or "\r" in label
        or "\n" in label
    ):
        raise ValueError("weight_table row needs a one-based index and an exact nonempty row label")
    return f"{material_id}/weight-table/{place}{selector['table']}/row-{selector['row']}"


def _header_unit(text, field):
    scope, kind = field.split("_", 1)
    name = _KIND[kind]
    if scope == "unit":
        label = rf"(?:{_ITEM})(?:{name})|(?:{name})(?:{_ITEM_SUFFIX})"
    elif scope == "line":
        label = rf"(?:{_LINE})(?:{name})"
    else:
        label = rf"(?:{_LINE})?(?:{name})"
    match = re.fullmatch(
        rf"(?:{label})\s*(?:\(\s*({_UNIT})\s*\)|\[\s*({_UNIT})\s*\])?", text.strip(), re.I
    )
    return match


def _unit_family(value):
    key = value.lower()
    return _UNIT_FAMILY.get(key, key)


def table_weight(blocks, execution, selector, field):
    """Resolve one scoped cell without promoting per-item or subtotal values."""
    profile = execution["executed_profile"]
    if "page" in selector:
        if profile["reader"] not in PDF_READERS:
            return None
    else:
        sheet = selector["worksheet"]
        observed = execution.get("result", {}).get("excel_evidence", {})
        if (
            profile["reader"] != "DoclingExcel"
            or profile.get("requested_scope", {}).get("sheet_names") != [sheet]
            or observed.get("selected_sheet") != sheet
            or observed.get("processed_sheet_names") != [sheet]
        ):
            return None
        bindings = [
            item
            for item in observed.get("docling_table_ranges", [])
            if item.get("table_ref") == "#" + selector["table"]
        ]
        if len(bindings) != 1 or bindings[0].get("sheet_name") != sheet:
            return None

    tables = {}
    for block in blocks:
        match = _CELL.fullmatch(block["pointer"])
        if match and block["text"].strip():
            tables.setdefault(match["table"], []).append(block)
    cells = tables.get(selector["table"], [])
    if not cells or (
        "page" in selector
        and any(
            len(block["provenance"]) != 1
            or block["provenance"][0].get("page_no") != selector["page"]
            for block in cells
        )
    ):
        return None
    for block in cells:
        row_start, row_end, col_start, col_end = (block["cell"].get(key) for key in _COORDS)
        if (
            any(type(x) is not int or x < 0 for x in (row_start, row_end, col_start, col_end))
            or row_end <= row_start
            or col_end <= col_start
        ):
            return None
    header_row = min(block["cell"][_COORDS[0]] for block in cells)
    if "row" in selector:
        row = selector["row"] - 1
        anchors = [
            block for block in cells if block["text"].strip() == selector["row_label"].strip()
        ]
        if (
            len(anchors) != 1
            or anchors[0]["cell"][_COORDS[0]] != row
            or any(
                block["cell"][_COORDS[0]] == row and _SUMMARY_PREFIX.match(block["text"].strip())
                for block in cells
            )
        ):
            return None
    else:
        if execution["execution_state"] != "success":
            return None
        markers = [
            block
            for blocks_in_table in tables.values()
            for block in blocks_in_table
            if _GRAND.fullmatch(block["text"].strip())
        ]
        if not markers and len(tables) == 1:
            markers = [
                block
                for block in cells
                if re.fullmatch(r"(?:total|总计|合计)\s*[:：]?", block["text"].strip(), re.I)
            ]
        if len(markers) != 1 or markers[0] not in cells:
            return None
        anchors = markers
        row = anchors[0]["cell"][_COORDS[0]]
    anchor = anchors[0]
    if row <= header_row or anchor["cell"][_COORDS[1]] != row + 1:
        return None
    headers = [
        (block, match)
        for block in cells
        if block["cell"][_COORDS[0]] == header_row
        and block["cell"][_COORDS[1]] == header_row + 1
        and (match := _header_unit(block["text"], field))
    ]
    if len(headers) != 1:
        return None
    header, match = headers[0]
    column = header["cell"][_COORDS[2]]
    if header["cell"][_COORDS[3]] != column + 1:
        return None
    values = [
        block
        for block in cells
        if block["cell"][_COORDS[0]] <= row < block["cell"][_COORDS[1]]
        and block["cell"][_COORDS[2]] <= column < block["cell"][_COORDS[3]]
    ]
    if len(values) != 1 or values[0] == anchor:
        return None
    value = values[0]
    if tuple(value["cell"][key] for key in _COORDS) != (row, row + 1, column, column + 1):
        return None
    parsed = _VALUE.fullmatch(value["text"])
    if not parsed:
        return None
    header_unit = next((item for item in match.groups() if item), None)
    cell_unit = parsed[2]
    if not (header_unit or cell_unit) or (
        header_unit and cell_unit and _unit_family(header_unit) != _unit_family(cell_unit)
    ):
        return None
    return Decimal(parsed[1]), cell_unit or header_unit, [value, header, anchor]

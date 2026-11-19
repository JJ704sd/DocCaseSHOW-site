"""Packing-list rows backed by READ table cells or unique PDF text positions."""

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from math import isfinite
from statistics import median
from urllib.parse import quote

from .pdf_profile import PDF_READERS, PDF_SCANNED, pdf_page_title

CARGO_ROW_PROFILE = "local-attachment-cargo-rows"
CARGO_ROW_FIELDS = (
    "product_name",
    "ctns",
    "length_cm",
    "width_cm",
    "height_cm",
    "line_net_weight",
    "line_gross_weight",
    "line_volume",
    "unit_gross_weight",
    "unit_net_weight",
)
_TABLE = re.compile(r"(?:/parts/\d+/document)?/tables/\d+")
_CELL = re.compile(r"(?P<table>(?:/parts/\d+/document)?/tables/\d+)/data/table_cells/\d+/text")
_TEXT = re.compile(r"(?P<part>/parts/\d+/document)?/texts/\d+/text")
_COL = "start_col_offset_idx"
_ROW = "start_row_offset_idx"
_HEADER_PATTERNS = {
    "item": r"item(?: no\.?)?|no\.?|序号",
    "product_name": r"item name|product(?: name)?|commodity|品名|货物品名",
    "description": r"description(?: of goods)?|货物描述",
    "ctns": r"cartons?|ctns?\.?|(?:no\.? of|total) cartons?|箱数",
    "item_quantity": r"(?:qty|quantity)\s*(?:\(\s*(?:pcs?|pieces?|sets?)\s*\)|(?:pcs?|pieces?|sets?))",
    "line_net_weight": r"(?:total |line )?(?:net weight|n\.?w\.?)\s*\(\s*kgs?\s*\)|净重\s*\(\s*kg\s*\)",
    "line_gross_weight": r"(?:total |line )?(?:gross weight|g\.?w\.?)\s*\(\s*kgs?\s*\)|毛重\s*\(\s*kg\s*\)",
    "unit_net_weight": r"(?:unit net weight|net weight per (?:item|unit))\s*\(\s*kgs?\s*\)",
    "unit_gross_weight": r"(?:unit gross weight|gross weight per (?:item|unit))\s*\(\s*kgs?\s*\)",
    "line_volume": r"cubic\s*\(cbm\)|volume\s*\(cbm\)|cbm|体积\s*\(cbm\)",
    "dimensions": r"(?:measurement|dimensions?|size)\s*\(\s*cm\s*\)|单箱尺寸\s*\(\s*cm\s*\)",
    **{
        field: rf"(?:carton |package )?{name}\s*\(\s*cm\s*\)"
        for name, field in (("length", "length_cm"), ("width", "width_cm"), ("height", "height_cm"))
    },
}
_NUM = re.compile(r"\d+(?:\.\d+)?")
_DIMS = re.compile(r"(\d+(?:\.\d+)?)\s*[*×xX]\s*(\d+(?:\.\d+)?)\s*[*×xX]\s*(\d+(?:\.\d+)?)")
_TOTAL = re.compile(r"^(?:total|subtotal|grand total)(?:\b|:)|^(?:合计|总计|小计)", re.I)
_HEADER_ALIASES = {
    "item": r"line no\.?|line #|n(?:ú|u)mero de l[ií]nea|行号",
    "product_name": r"descripci[oó]n (?:de (?:la )?mercanc[ií]a|del producto)|nombre del producto|货品名称",
    "ctns": r"(?:no\.? of |number of )?(?:cartons?|cajas de cart[oó]n)|纸箱数(?:量)?",
    "line_net_weight": r"(?:total )?peso neto\s*\(kgs?\)|净重\s*\(公斤\)",
    "line_gross_weight": r"(?:total )?peso bruto\s*\(kgs?\)|毛重\s*\(公斤\)",
    "line_volume": r"volumen\s*\((?:m[³3]|cbm)\)|体积\s*\((?:m[³3]|立方米)\)",
}
_WEIGHT_PARENT = re.compile(
    r"(?:(?P<item>per (?:item|product) |单件)(?:weights?|peso|重量)|"
    r"(?:line |total )?(?:weights?|peso|重量))(?:\s*\(\s*(?P<kg>kgs?|公斤)\s*\))?",
    re.I,
)
_CARTON_DIM_PARENT = re.compile(r"(?:carton (?:size|dimensions?)|单箱尺寸)\s*\(\s*cm\s*\)", re.I)
_GENERAL_HEADERS = {
    k: re.compile(
        rf"(?:{pattern})|(?:{_HEADER_ALIASES[k]})" if k in _HEADER_ALIASES else pattern, re.I
    )
    for k, pattern in _HEADER_PATTERNS.items()
}

# A second header layout must prove the package and measurement roles itself.
# Do not infer weight units from a different column or turn PCS into cartons.
_ALT_HEADERS = (
    (
        "carton_range",
        {
            "item": r"ctn no\.? 箱号",
            "product_name": r"products description 产品描述",
            "ctns": r"(?:date:\s*\d{4}-\d{2}-\d{2}\s+)?qty 箱子数量",
            "dimensions": r"size 箱尺寸\(厘米\)",
            "line_volume": r"cbm 立方米",
        },
    ),
    (
        "carton_columns",
        {
            "item": r"seq",
            "product_name": r"description 描述",
            "ctns": r"ctn\s*/\s*箱数",
            "dimensions": r"size\s*/\s*尺寸\s*\(cm\)",
            "line_volume": r"cbm\s*\(体积\)",
        },
    ),
    (
        "carton_export",
        {
            "item": r"item no\.?",
            "product_name": r"name",
            "ctns": r"ctns",
            "dimensions": r"exp\. carton\s*\(cm\)",
            "line_volume": r"t\s*/\s*cbm",
            "line_net_weight": r"t\s+n\.w\s+kgs?",
            "line_gross_weight": r"(?:date:\s*\d{4}[-/]\d{2}[-/]\d{2}\s+)?t\s+g\.w\s+kgs?",
        },
    ),
    (
        "pallet_manifest",
        {
            "item": r"item\s*#",
            "product_name": r"item description",
            "piece_quantity": r"qty pcs",
            "line_gross_weight": r"g\.w\. per plt kgs?",
            "line_volume": r"cbm",
            "package_dimensions": r"packing size\(m\)",
        },
    ),
)


def cargo_row_object(mid, selector):
    if (
        not isinstance(selector, dict)
        or type(selector.get("row")) is not int
        or selector["row"] < 1
    ):
        raise ValueError("Cargo row needs a positive one-based row")
    if (
        set(selector) == {"page", "anchor", "row"}
        and type(selector["page"]) is int
        and selector["page"] >= 1
    ):
        if not isinstance(selector["anchor"], str) or not _TEXT.fullmatch(selector["anchor"]):
            raise ValueError("Positioned cargo row needs an exact PDF READ text anchor")
        return f"{mid}/cargo-position/page-{selector['page']}{selector['anchor']}/row-{selector['row']}"
    if not _TABLE.fullmatch(str(selector.get("table"))):
        raise ValueError("Cargo row needs a READ table")
    if (
        set(selector) == {"page", "table", "row"}
        and type(selector["page"]) is int
        and selector["page"] >= 1
    ):
        location = f"page-{selector['page']}"
    elif (
        set(selector) == {"worksheet", "table", "row"}
        and isinstance(selector["worksheet"], str)
        and (
            0 < len(selector["worksheet"].strip()) <= 31
            and "\n" not in selector["worksheet"]
            and "\r" not in selector["worksheet"]
            and not selector["table"].startswith("/parts/")
        )
    ):
        location = "worksheet-" + quote(selector["worksheet"], safe="")
    else:
        raise ValueError("Cargo row needs one page or worksheet")
    return f"{mid}/cargo-table/{location}{selector['table']}/row-{selector['row']}"


def _location(table, cells, execution):
    reader = execution["executed_profile"]["reader"]
    if reader == "DoclingExcel":
        evidence = execution.get("result", {}).get("excel_evidence", {})
        sheet = evidence.get("selected_sheet")
        if (
            sheet
            and evidence.get("processed_sheet_names") == [sheet]
            and execution["executed_profile"].get("requested_scope", {}).get("sheet_names")
            == [sheet]
            and sum(
                x.get("sheet_name") == sheet and x.get("table_ref") == "#" + table
                for x in evidence.get("docling_table_ranges", [])
            )
            == 1
        ):
            return {"worksheet": sheet}
    elif reader in PDF_READERS:
        pages = {
            b["provenance"][0].get("page_no") for b in cells if len(b.get("provenance", [])) == 1
        }
        if (
            len(pages) == 1
            and None not in pages
            and all(len(b.get("provenance", [])) == 1 for b in cells)
        ):
            return {"page": pages.pop()}
    return None


def _general_headers(rows, y):
    """Bind each leaf column to the real header cells that prove its meaning."""
    top, bottom = rows[y], rows.get(y + 1, ())
    # Cells beginning on a second heading row can stand under vertically or
    # horizontally merged labels. Data cells are never used as header text.
    columns = defaultdict(dict)
    for block in top:
        cell = block["cell"]
        if cell["end_col_offset_idx"] != cell[_COL] + 1:
            continue
        for field, pattern in _GENERAL_HEADERS.items():
            if pattern.fullmatch(_header_text(block)):
                columns[field].setdefault(cell[_COL], []).append((block,))
    for leaf in bottom:
        c, text = leaf["cell"], _header_text(leaf)
        col = c[_COL]
        if c["end_col_offset_idx"] != col + 1 or c["end_row_offset_idx"] != y + 2:
            continue
        parents = [
            b
            for b in top
            if b["cell"][_COL] <= col < b["cell"]["end_col_offset_idx"]
            and b["cell"]["end_row_offset_idx"] == y + 1
        ]
        if len(parents) != 1:
            continue
        parent = parents[0]
        label = _header_text(parent)
        if match := _WEIGHT_PARENT.fullmatch(label):
            scope = "unit" if match["item"] else "line"
            for kind, pattern in (
                ("net", r"net|neto|净重|n\.?w\.?"),
                ("gross", r"gross|bruto|毛重|g\.?w\.?"),
            ):
                parts = re.fullmatch(rf"(?:{pattern})(?:\s*\(\s*(kgs?|公斤)\s*\))?", text, re.I)
                if parts and (match["kg"] or parts[1]):
                    columns[f"{scope}_{kind}_weight"].setdefault(col, []).append((parent, leaf))
        elif _CARTON_DIM_PARENT.fullmatch(label):
            for field, pattern in (
                ("length_cm", "l|length|长"),
                ("width_cm", "w|width|宽"),
                ("height_cm", "h|height|高"),
            ):
                if re.fullmatch(pattern, text, re.I):
                    columns[field].setdefault(col, []).append((parent, leaf))
        elif parent["cell"][_COL] == col and parent["cell"]["end_col_offset_idx"] == col + 1:
            for field in ("line_net_weight", "line_gross_weight", "line_volume"):
                if re.fullmatch(_HEADER_PATTERNS[field], f"{label} ({text.strip('()')})", re.I):
                    columns[field].setdefault(col, []).append((parent, leaf))
    if not columns["product_name"]:
        columns["product_name"] = columns["description"]
    head = {
        f: (col, proofs[0])
        for f, matches in columns.items()
        if len(matches) == 1 and f != "description"
        for col, proofs in matches.items()
        if len(proofs) == 1
    }
    if len({col for col, _ in head.values()}) != len(head):
        return {}
    return head


def _row_header_bands(rows):
    """Find compatible repeated headers without carrying columns across tables."""
    bands = []
    for y in sorted(rows):
        if bands and y < bands[-1][1]:
            continue
        head = _general_headers(rows, y)
        if {"item", "product_name", "ctns"} <= head.keys() and {
            "line_gross_weight",
            "line_net_weight",
            "unit_gross_weight",
            "unit_net_weight",
            "dimensions",
            "line_volume",
            "length_cm",
        } & head.keys():
            after = max(
                b["cell"]["end_row_offset_idx"] for _, proof in head.values() for b in proof
            )
            bands.append((y, after, head))
    if bands and any(
        {f: col for f, (col, _) in h.items()} != {f: col for f, (col, _) in bands[0][2].items()}
        for _, _, h in bands[1:]
    ):
        return []
    return bands


def _number(text):
    v = Decimal(text)
    return int(v) if v == v.to_integral_value() else float(v)


_ROW_UNITS = {
    "ctns": r"ctns?\.?|cartons?|纸箱",
    "cm": r"cm|厘米",
    "CBM": r"cbm|m[³3]|立方米",
    "kg": r"kgs?|公斤",
    "piece": r"pcs?|pieces?|件",
    "set": r"sets?|套",
}


def _row_number(text, unit):
    """Interpret only a value compatible with the header's already proven unit."""
    if len(text) > 42:
        return None
    text = unicodedata.normalize("NFKC", text)
    match = re.fullmatch(rf"([0-9.,]+)(?:\s*(?:{_ROW_UNITS[unit]}))?", text, re.I)
    if not match:
        return None
    value = match[1]
    if re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]+", value):
        value = value.replace(",", "")
    elif re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]+", value):
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"[0-9]+,(?:[0-9]{1,2}|[0-9]{4,})", value) or re.fullmatch(
        r"0,[0-9]{3}", value
    ):
        value = value.replace(",", ".")
    # A single comma with three digits after a nonzero integer has two valid
    # readings without a proven locale, so leave it unresolved.
    if not _NUM.fullmatch(value):
        return None
    number = _number(value)
    return None if unit == "ctns" and type(number) is not int else number


def _header_text(block):
    return " ".join(unicodedata.normalize("NFKC", block["text"]).split()).casefold()


def _alternate_rows(table, rows, place, title):
    headers = []
    for y, cells in rows.items():
        for variant, patterns in _ALT_HEADERS:
            if _header_text(title) == "pl" and variant != "pallet_manifest":
                continue
            head = {}
            for name, pattern in patterns.items():
                candidates = [
                    b
                    for b in cells
                    if re.fullmatch(pattern, _header_text(b))
                    and b["cell"]["end_row_offset_idx"] == y + 1
                ]
                if len(candidates) == 1:
                    cell = candidates[0]["cell"]
                    width = cell["end_col_offset_idx"] - cell[_COL]
                    if width == 1 or name in {"dimensions", "package_dimensions"} and width == 3:
                        head[name] = candidates[0]
            required = (
                patterns.keys()
                if variant == "pallet_manifest"
                else {"item", "product_name", "ctns", "dimensions", "line_volume"}
            )
            if required <= head.keys():
                headers.append((y, variant, head))
    if len(headers) != 1:
        return
    y, variant, head = headers[0]
    ordinal = 0
    anchors = [
        _header_text(b)
        for r, cells in rows.items()
        if r > y
        for b in cells
        if b["cell"][_COL] == head["item"]["cell"][_COL]
    ]
    for r in sorted(i for i in rows if i > y):
        cells = rows[r]
        if any(_TOTAL.search(b["text"].strip()) for b in cells):
            break
        cols = defaultdict(list)
        for b in cells:
            c = b["cell"]
            if c["end_row_offset_idx"] == r + 1 and c["end_col_offset_idx"] == c[_COL] + 1:
                cols[c[_COL]].append(b)

        def at(i, cols=cols):
            return cols[i][0] if len(cols[i]) == 1 else None

        anchor = at(head["item"]["cell"][_COL])
        product = at(head["product_name"]["cell"][_COL])
        if not product or len(product["text"].strip()) > 160:
            continue
        key = _header_text(anchor) if anchor else ""
        if variant == "pallet_manifest":
            qty = at(head["piece_quantity"]["cell"][_COL])
            if not qty or not re.fullmatch(r"[1-9]\d*", qty["text"].strip()):
                continue
        elif not (
            re.fullmatch(
                r"[1-9]\d*(?:\s*[-–]\s*[1-9]\d*)?" if variant == "carton_range" else r"[1-9]\d*",
                key,
            )
            and anchors.count(key) == 1
        ):
            continue
        ordinal += 1
        facts = {}

        def add(name, value, unit, b, header, facts=facts, anchor=anchor):
            proof = [b, header, title]
            if anchor is not None and anchor is not b:
                proof.insert(2, anchor)
            facts[name] = value, unit, proof

        add("product_name", product["text"].strip(), None, product, head["product_name"])
        for name in ("ctns", "line_volume", "line_net_weight", "line_gross_weight"):
            if name not in head or not (b := at(head[name]["cell"][_COL])):
                continue
            number = b["text"].strip()
            if variant == "pallet_manifest":
                if not re.fullmatch(r"plt [1-9]\d*", key) or anchors.count(key) != 1:
                    continue
                number = number.replace(",", ".")
            if not _NUM.fullmatch(number) or (
                name == "ctns" and not re.fullmatch(r"\d+(?:\.0+)?", number)
            ):
                continue
            add(
                name,
                _number(number),
                None if name == "ctns" else "CBM" if name == "line_volume" else "kg",
                b,
                head[name],
            )
        if "dimensions" in head:
            dim = head["dimensions"]
            first, stop = dim["cell"][_COL], dim["cell"]["end_col_offset_idx"]
            dims = [at(i) for i in range(first, stop)]
            if len(dims) == 1 and dims[0] and (match := _DIMS.fullmatch(dims[0]["text"].strip())):
                parts = list(zip(match.groups(), dims * 3, strict=True))
            elif len(dims) == 3 and all(b and _NUM.fullmatch(b["text"].strip()) for b in dims):
                parts = [(b["text"].strip(), b) for b in dims]
            else:
                parts = []
            for name, (value, b) in zip(
                ("length_cm", "width_cm", "height_cm"), parts, strict=False
            ):
                add(name, _number(value), "cm", b, dim)
        yield {
            "selector": {**place, "table": table, "row": r + 1},
            "ordinal": int(key) if variant in {"carton_columns", "carton_export"} else ordinal,
            "facts": facts,
        }


_POSITION_HEADERS = {
    "product_name": rf"{_HEADER_PATTERNS['product_name']}|{_HEADER_PATTERNS['description']}",
    "model": r"model(?: no\.?)?|sku|型号",
    "ctns": _HEADER_PATTERNS["ctns"],
    "line_net_weight": r"(?:line|total) net weight\s*\(\s*kgs?\s*\)",
    "line_gross_weight": r"(?:line|total) gross weight\s*\(\s*kgs?\s*\)",
    "unit_net_weight": _HEADER_PATTERNS["unit_net_weight"],
    "unit_gross_weight": _HEADER_PATTERNS["unit_gross_weight"],
    "line_volume": r"(?:line|total) volume\s*\(\s*cbm\s*\)",
    "dimensions": r"(?:single )?carton (?:measurement|dimensions?|size)\s*\(\s*cm\s*\)|单箱尺寸\s*\(\s*cm\s*\)",
}


def _position_box(provenance):
    box = provenance.get("bbox")
    if not isinstance(box, dict):
        return None
    lft, rgt, top, bottom = (box.get(key) for key in ("l", "r", "t", "b"))
    if not all(type(v) in {int, float} and isfinite(v) for v in (lft, rgt, top, bottom)):
        return None
    if box.get("coord_origin") == "TOPLEFT":
        top, bottom = -top, -bottom
    elif box.get("coord_origin") != "BOTTOMLEFT":
        return None
    return (lft, rgt, top, bottom) if lft < rgt and bottom < top else None


def _positioned_page_rows(page, items):
    """Bind fields to exactly one named row and one unambiguous header column."""
    if len(items) > 1500:
        return
    titles = [i for i, (b, _) in enumerate(items) if pdf_page_title(b["text"]) == "packing"]
    if len(titles) != 1 or any(pdf_page_title(b["text"]) == "invoice" for b, _ in items):
        return
    title = titles[0]
    positions = [box for _, box in items]
    texts = [_header_text(b) for b, _ in items]

    def height(i):
        return positions[i][2] - positions[i][3]

    def y(i):
        return (positions[i][2] + positions[i][3]) / 2

    def overlaps(i, j):
        return min(positions[i][2], positions[j][2]) > max(positions[i][3], positions[j][3])

    def same_line(i, j):
        return overlaps(i, j) and abs(y(i) - y(j)) <= min(height(i), height(j)) / 2

    roles = [
        next(
            (name for name, pattern in _POSITION_HEADERS.items() if re.fullmatch(pattern, text)),
            None,
        )
        for text in texts
    ]
    header_lines = {
        tuple(
            sorted((i for i in range(len(items)) if same_line(i, j)), key=lambda i: positions[i][0])
        )
        for j, role in enumerate(roles)
        if role in {"product_name", "model"} and y(j) < y(title)
    }
    if len(header_lines) != 1:
        return
    heads = next(iter(header_lines))
    if any(positions[a][1] > positions[b][0] for a, b in zip(heads, heads[1:], strict=False)):
        return
    named = {
        roles[h]: h for h in heads if roles[h] and sum(roles[i] == roles[h] for i in heads) == 1
    }
    anchor_head = named.get("model", named.get("product_name"))
    if anchor_head is None or not (named.keys() - {"model", "product_name"}):
        return
    limits = [
        (positions[a][1] + positions[b][0]) / 2 for a, b in zip(heads, heads[1:], strict=False)
    ]
    limits = [positions[heads[0]][0] - 8, *limits, positions[heads[-1]][1] + 8]
    bands = {h: (lo, hi) for h, lo, hi in zip(heads, limits[:-1], limits[1:], strict=True)}

    def in_column(i, h):
        lo, hi = bands[h]
        return lo <= positions[i][0] < positions[i][1] <= hi

    header_bottom = min(positions[i][3] for i in heads)
    footer_top = max(
        (
            positions[i][2]
            for i, text in enumerate(texts)
            if _TOTAL.match(text)
            and positions[i][2] <= header_bottom
            and limits[0] <= positions[i][0] < limits[-1]
        ),
        default=float("-inf"),
    )
    body = [
        i
        for i, text in enumerate(texts)
        if text and footer_top <= positions[i][3] and positions[i][2] <= header_bottom
    ]
    # Retain all blocks in the identity column when checking for possible overlap;
    # an untrusted or duplicate name must not make its neighbor's number easier to claim.
    possible = [i for i in body if in_column(i, anchor_head)]
    names = [texts[i] for i in possible]
    for ordinal, anchor in enumerate(sorted(possible, key=y, reverse=True), 1):
        if (
            names.count(texts[anchor]) != 1
            or not any(c.isalpha() for c in texts[anchor])
            or len(texts[anchor]) > 160
            or "\n" in items[anchor][0]["text"]
            or any(i != anchor and overlaps(i, anchor) for i in possible)
        ):
            continue
        facts = {}
        for name, h in named.items():
            if name == "model":
                continue
            matches = [i for i in body if in_column(i, h) and same_line(i, anchor)]
            if len(matches) != 1:
                continue
            i = matches[0]
            if height(i) > 1.5 * height(anchor) or any(
                a != anchor and overlaps(a, i) for a in possible
            ):
                continue
            block = items[i][0]
            text = block["text"].strip()
            proof = [block, items[h][0], items[title][0]]
            if i != anchor:
                proof.insert(2, items[anchor][0])
            if name == "product_name":
                if len(text) <= 160 and "\n" not in text and any(c.isalpha() for c in text):
                    facts[name] = text, None, proof
            elif name == "dimensions":
                if dims := _DIMS.fullmatch(text):
                    for axis, value in zip(
                        ("length_cm", "width_cm", "height_cm"), dims.groups(), strict=True
                    ):
                        facts[axis] = _number(value), "cm", proof
            elif _NUM.fullmatch(text) and (name != "ctns" or text.isdigit()):
                facts[name] = (
                    _number(text),
                    None if name == "ctns" else "CBM" if name == "line_volume" else "kg",
                    proof,
                )
        if facts:
            yield {
                "selector": {"page": page, "anchor": items[anchor][0]["pointer"], "row": ordinal},
                "ordinal": ordinal,
                "facts": facts,
            }


def _positioned_rows(blocks):
    pages = defaultdict(lambda: defaultdict(list))
    table_pages = set()
    for b in blocks:
        prov = b.get("provenance", [])
        if b.get("cell"):
            if not prov or any(type(p.get("page_no")) is not int for p in prov):
                return  # Cannot prove the table and text describe different pages.
            table_pages.update(p["page_no"] for p in prov)
        elif (
            len(prov) == 1
            and type(page := prov[0].get("page_no")) is int
            and page > 0
            and (ref := _TEXT.fullmatch(b["pointer"]))
        ):
            if box := _position_box(prov[0]):
                pages[page][ref["part"]].append((b, box))
            elif pdf_page_title(b["text"]) == "invoice":
                table_pages.add(page)
    for page, parts in pages.items():
        if page not in table_pages and len(parts) == 1:
            yield from _positioned_page_rows(page, next(iter(parts.values())))


def _table_sections(table, rows, execution):
    cells = [b for group in rows.values() for b in group]
    if place := _location(table, cells, execution):
        yield place, rows
        return
    if execution["executed_profile"]["reader"] not in PDF_READERS:
        return
    # A Docling table can span pages. Keep the original table and row pointers,
    # but interpret only page sections whose own header proves the column roles.
    by_page = defaultdict(dict)
    for row, group in rows.items():
        pages = [b.get("provenance", ()) for b in group]
        if any(
            len(p) != 1 or type(p[0].get("page_no")) is not int or p[0]["page_no"] < 1
            for p in pages
        ):
            return
        unique = {p[0]["page_no"] for p in pages}
        if len(unique) != 1:
            return
        by_page[unique.pop()][row] = group
    for page, section in sorted(by_page.items()):
        if _row_header_bands(section):
            yield {"page": page}, section


def _recover_positioned_net_weights(output, rows, head, blocks, page):
    """Use independently positioned READ text when a table lost a net-weight cell."""
    if "line_net_weight" not in head or not any(
        "line_net_weight" not in row["facts"] for row in output
    ):
        return
    parts = defaultdict(list)
    for block in blocks:
        match = _TEXT.fullmatch(block.get("pointer", ""))
        prov = block.get("provenance", [])
        if (
            not match
            or block.get("pdf_pass") == "ocr"
            or len(prov) != 1
            or prov[0].get("page_no") != page
        ):
            continue
        if box := _position_box(prov[0]):
            parts[match["part"]].append((block, box))
    eligible = []
    for candidates in parts.values():
        matches = 0
        for row in output:
            product = row["facts"].get("product_name")
            if not product:
                continue
            name = " ".join(product[0].casefold().split())
            if (
                sum(
                    bool(re.match(rf"\s*{row['ordinal']}\s+", b["text"]))
                    and name in " ".join(b["text"].casefold().split())
                    for b, _ in candidates
                )
                == 1
            ):
                matches += 1
        if matches >= 3:
            eligible.append(candidates)
    if len(eligible) != 1:
        return
    native = eligible[0]

    def same_line(a, b):
        ay, by = (a[2] + a[3]) / 2, (b[2] + b[3]) / 2
        return (
            min(a[2], b[2]) > max(a[3], b[3]) and abs(ay - by) <= min(a[2] - a[3], b[2] - b[3]) / 2
        )

    counts = defaultdict(int)
    for row in output:
        counts[row["ordinal"]] += 1
    anchors = {}
    for row in output:
        product = row["facts"].get("product_name")
        if counts[row["ordinal"]] != 1 or not product:
            continue
        name = " ".join(product[0].casefold().split())
        matches = [
            (b, box)
            for b, box in native
            if re.match(rf"\s*{row['ordinal']}\s+", b["text"])
            and name in " ".join(b["text"].casefold().split())
        ]
        if len(matches) == 1:
            anchors[row["selector"]["row"]] = matches[0]
    calibrated = []
    for row in output:
        anchor = anchors.get(row["selector"]["row"])
        fact = row["facts"].get("line_net_weight")
        if not anchor or not fact:
            continue
        matches = [
            (b, box)
            for b, box in native
            if same_line(anchor[1], box) and _row_number(b["text"].strip(), "kg") == fact[0]
        ]
        if len(matches) == 1:
            calibrated.append((matches[0], fact[2][0], fact[0]))
    if len(calibrated) < 3:
        return
    centers = [(item[0][1][0] + item[0][1][1]) / 2 for item in calibrated]
    center = median(centers)
    tolerance = max(4, median(item[0][1][2] - item[0][1][3] for item in calibrated))
    stable = [
        item for item, x in zip(calibrated, centers, strict=True) if abs(x - center) <= tolerance
    ]
    if len(stable) < 3 or len({item[2] for item in stable}) < 2:
        return
    column, header = head["line_net_weight"]
    all_cells = [b for cells in rows.values() for b in cells]
    for row in output:
        anchor = anchors.get(row["selector"]["row"])
        index = row["selector"]["row"] - 1
        if not anchor or "line_net_weight" in row["facts"]:
            continue
        covering = [
            b
            for b in all_cells
            if b["cell"][_ROW] <= index < b["cell"]["end_row_offset_idx"]
            and b["cell"][_COL] <= column < b["cell"]["end_col_offset_idx"]
        ]
        if len(covering) > 1 or any(
            b["cell"]["end_row_offset_idx"] != b["cell"][_ROW] + 1
            or b["cell"]["end_col_offset_idx"] != b["cell"][_COL] + 1
            for b in covering
        ):
            continue
        candidates = [
            (b, box)
            for b, box in native
            if same_line(anchor[1], box)
            and abs((box[0] + box[1]) / 2 - center) <= tolerance
            and _row_number(b["text"].strip(), "kg") is not None
            and not any(
                other is not anchor and same_line(other[1], box) for other in anchors.values()
            )
        ]
        if len(candidates) != 1:
            continue
        block = candidates[0][0]
        if covering and block["text"].strip() not in covering[0]["text"].split():
            continue
        proof = [block, *header, row["facts"]["product_name"][2][-1], anchor[0]]
        for (sample, _), table_value, _ in stable[:2]:
            proof.extend((sample, table_value))
        row["facts"]["line_net_weight"] = (_row_number(block["text"].strip(), "kg"), "kg", proof)


def packing_rows(blocks, execution):
    """Prefer table cells. Use position proofs on PDF pages without a table."""
    invoice_pages = set()
    if execution["executed_profile"]["reader"] == PDF_SCANNED:
        invoice_pages = {
            b["provenance"][0]["page_no"]
            for b in blocks
            if b.get("pdf_pass") == "ocr"
            and pdf_page_title(b["text"]) == "invoice"
            and len(b.get("provenance", ())) == 1
            and "page_no" in b["provenance"][0]
        }
    tables = defaultdict(lambda: defaultdict(list))
    for b in blocks:
        if b.get("cell") and b["text"].strip() and (m := _CELL.fullmatch(b["pointer"])):
            tables[m["table"]][b["cell"][_ROW]].append(b)
    sections = (
        (table, place, section)
        for table, rows in tables.items()
        for place, section in _table_sections(table, rows, execution)
    )
    for table, place, rows in sections:
        if place.get("page") in invoice_pages:
            continue
        bands = _row_header_bands(rows)
        if not bands:
            if "page" in place:
                page = place["page"]
                titled = [
                    b
                    for b in blocks
                    if len(b.get("provenance", ())) == 1
                    and b["provenance"][0].get("page_no") == page
                    and (
                        pdf_page_title(b["text"]) == "packing"
                        or (_header_text(b) == "pl" and b["pointer"].startswith(table + "/"))
                    )
                ]
                if len(titled) == 1:
                    yield from _alternate_rows(table, rows, place, titled[0])
            continue
        extracted = list(_rows_from_bands(table, rows, place, bands))
        if "page" in place and len(bands) == 1:
            _recover_positioned_net_weights(extracted, rows, bands[0][2], blocks, place["page"])
        yield from extracted
    if execution["executed_profile"]["reader"] in PDF_READERS:
        yield from _positioned_rows(blocks)


def _rows_from_bands(table, rows, place, bands):
    ordinal = 0
    for i, (_, after, head) in enumerate(bands):
        stop = bands[i + 1][0] if i + 1 < len(bands) else float("inf")
        for row in sorted(r for r in rows if after <= r < stop):
            cells = rows[row]
            if any(
                _TOTAL.search(b["text"].strip())
                for b in cells
                if b["cell"][_COL] <= head["ctns"][0]
            ):
                break
            by_col = defaultdict(list)
            for b in cells:
                if (
                    b["cell"]["end_row_offset_idx"] == row + 1
                    and b["cell"]["end_col_offset_idx"] == b["cell"][_COL] + 1
                ):
                    by_col[b["cell"][_COL]].append(b)

            def cell(name, by_col=by_col, head=head):
                if name in head and len(items := by_col[head[name][0]]) == 1:
                    return items[0]
                return None

            anchor, product = cell("item"), cell("product_name")
            if not anchor or not product or len(product["text"].strip()) > 160:
                continue
            source_no = anchor["text"].strip()
            if ("page" in place and not re.fullmatch(r"[1-9]\d{0,3}", source_no)) or (
                "worksheet" in place and not re.fullmatch(r"[\w.-]{1,60}", source_no)
            ):
                continue
            ordinal += 1
            facts = {}
            evidence_spans = {}
            for name in ("line_net_weight", "line_gross_weight", "line_volume"):
                if name in head and (target := cell(name)):
                    evidence_spans[name] = {
                        "value": target,
                        "headers": head[name][1],
                        "identity": anchor,
                    }
            for name in (*CARGO_ROW_FIELDS, "item_quantity"):
                kind = (
                    "dimensions"
                    if name in {"length_cm", "width_cm", "height_cm"} and "dimensions" in head
                    else name
                )
                if not (b := cell(kind)):
                    continue
                text = b["text"].strip()
                if name == "product_name":
                    value, unit = text, None
                elif kind == "dimensions":
                    if not (dims := _DIMS.fullmatch(text)):
                        continue
                    value, unit = (
                        _number(dims[{"length_cm": 1, "width_cm": 2, "height_cm": 3}[name]]),
                        "cm",
                    )
                elif name == "item_quantity":
                    unit = (
                        "set"
                        if re.search(r"\bsets?\b", _header_text(head[kind][1][0]))
                        else "piece"
                    )
                    value = _row_number(text, unit)
                    if value is None:
                        continue
                else:
                    unit = (
                        None
                        if name == "ctns"
                        else "CBM"
                        if name == "line_volume"
                        else "cm"
                        if name.endswith("_cm")
                        else "kg"
                    )
                    value = _row_number(text, unit or "ctns")
                    if value is None:
                        continue
                facts[name] = (value, unit, [b, *head[kind][1], anchor])
            yield {
                "selector": {**place, "table": table, "row": row + 1},
                "ordinal": int(source_no) if "page" in place else ordinal,
                "facts": facts,
                "evidence_spans": evidence_spans,
            }


def agent_evidence_spans(row, locate):
    """Serialize a target cell, its verified header and the same-row identity."""
    result = []
    for field, parts in row.get("evidence_spans", {}).items():
        blocks = [("header", block) for block in parts["headers"]]
        blocks.extend((("value", parts["value"]), ("identity", parts["identity"])))
        locations = [locate(block) for _, block in blocks]
        if any(location is None for location in locations):
            continue
        result.extend(
            {
                "field": field,
                "position": position,
                "pointer": block["pointer"],
                "excerpt": block["text"],
                "kind": "attachment",
                **location,
            }
            for (position, block), location in zip(blocks, locations, strict=True)
        )
    return result


def cargo_row_profile(mid, task_id, rows):
    requirements = []
    for i, row in enumerate(rows, 1):
        selector = row["selector"]
        for field in CARGO_ROW_FIELDS:
            requirements.append(
                {
                    "requirement_id": f"ROW-{i}-{field}",
                    "material_id": mid,
                    "field_name": field,
                    "object_selector": cargo_row_object(mid, selector),
                    "cargo_table_row": selector,
                    "source_kind": "attachment",
                    "critical": False,
                    "missing_policy": "allow",
                    "allow_not_applicable": False,
                    "scope_dependencies": [
                        {"stage": "extraction", "refs": [mid + "/representation"]}
                    ],
                }
            )
    return {
        "id": CARGO_ROW_PROFILE,
        "version": "1",
        "task_scope_ref": task_id,
        "requirements": requirements,
    }

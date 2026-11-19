"""Bounded READ context for fields that need more than a single excerpt."""

import re
from collections import defaultdict

_FAMILIES = (
    (
        frozenset({"cargo_origin", "pickup_city", "departure_port"}),
        re.compile(
            r"\b(?:origin|pickup|pick[- ]up|ship from|loading|aol|pol|exw|fob|airport)\b|货在|货源|发货|提货|起运|装货",
            re.I,
        ),
    ),
    (
        frozenset({"ctns", "package_count", "package_type"}),
        re.compile(
            r"\b(?:qty|packing|quantity|cartons?|ctns?|boxes?|pallets?|plts?|packages?|pkgs?|pcs)\b|"
            r"\d\s*(?:ctns?|pkgs?|plts?)\b|箱数|托盘|包装|\d\s*[箱托件]",
            re.I,
        ),
    ),
    (
        frozenset(
            {
                "shipment_gross_weight",
                "shipment_net_weight",
                "shipment_chargeable_weight",
                "line_gross_weight",
            }
        ),
        re.compile(
            r"\b(?:weight|gross|net|chargeable|kgs?|cw|g\.w\.|n\.w\.)\b|\d\s*kgs?\b|毛重|净重|计费重|公斤",
            re.I,
        ),
    ),
    (
        frozenset({"line_volume"}),
        re.compile(
            r"\b(?:total volume|volume|cubage|cbm|volumen|volume total|m3)\b|m³|立方米|体积", re.I
        ),
    ),
    (
        frozenset({"cargo_ready_time"}),
        re.compile(
            r"\b(?:cargo ready|ready (?:date|for pickup|on|by)|collection date|fecha de recogida)\b|货好|备妥|可提货",
            re.I,
        ),
    ),
    (
        frozenset({"business_remarks"}),
        re.compile(
            r"\b(?:partial shipments?|split shipments?|release (?:only|after|upon)|no batteries|contains batteries|"
            r"do not release|must (?:ship|release))\b|分批|分票|放货|带电|不带电|含电池",
            re.I,
        ),
    ),
)
SEMANTIC_CONTEXT_FIELDS = frozenset.union(*(family for family, _ in _FAMILIES))
_FIELD_CUE = {field: pattern for family, pattern in _FAMILIES for field in family}
_SIGNOFF = re.compile(r"^\s*(?:thanks(?:\s+and)?\s+)?(?:best\s+|kind\s+)?regards\b|^\s*顺祝", re.I)
_FENCE = re.compile(
    r"^\s*(?:from|to|cc|subject|sent|fax|tel|phone|mail|e-mail|mobile|add(?:ress)?|"
    r"联系人|联系电话|发件人|收件人|抄送|主题|传真)\s*[:：]"
    r"|^\s*(?:emailbridge\b|hi\b|dear\b|hello\b|notes?\s*[:：]|other charges\b)"
    r"|\b(?:iban|swift|bank account|quote applies|rates? (?:are )?based on|rate quote|adjust my rates|special rate)\b"
    r"|\b(?:customer service|air pricing)\b|银行账户|报价适用|交货价|提货费|本文件由邮件服务器"
    r"|^\s*(?:pick[- ]?up|delivery|loading|warehouse)\s+(?:address|location)\s*[:：]|(?:提货|收货|装货)地址",
    re.I,
)
_CELL = re.compile(r"(?P<table>/tables/\d+)/data/table_cells/\d+/text")
_SAFE_TABLE_LABEL = re.compile(
    r"\b(?:gross|net|weight|chargeable|origin|pickup|airport|cartons?|ctns?|qty)\b|"
    r"\b(?:volume|cbm|ready date|packing)\b|"
    r"货源地|提货城市|起运港|毛重|净重|计费重|箱数|包装件数|包装类型|体积|货好时间",
    re.I,
)
_RELATED = re.compile(
    r"\b(?:cargo|shipment|booking|terms|dimensions?|measures?|volume|cbm)\b|"
    r"尺寸|品名|货物|箱单|发票|货好|\d\s*cbm\b|^[A-Z]{3}[ \t]*[-–][ \t]*[A-Z]{3}$",
    re.I,
)
_VALUE_AFTER_LABEL = re.compile(r"[\w.\-]+(?:[ \t]+[\w.\-]+){0,4}")
_CONTINUATION = re.compile(
    r"^(?:\d+(?:[.,]\d+)?\s*(?:cbm|m[³3])|(?:纸箱|木箱|托盘)|(?:cartons?|crates?|pallets?)|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:今天|明天|后天)(?:上午|下午|晚上)?|"
    r"(?:this|tomorrow)\s+(?:morning|afternoon)|(?:yes|no|allowed|not allowed))\s*[.!。]?$",
    re.I,
)
_SCOPE_CHANGE = re.compile(
    r"^\s*(?:(?:option|shipment|lot|booking)\s+(?:[A-Z]|\d+)\b|(?:备选|方案|第[一二三四五\d]+票)|"
    r"(?:on|el) .{0,120} (?:wrote|escribi[oó])\s*:|-----|>)",
    re.I,
)


def semantic_context_spans(blocks, current, allow, fields):
    """Return field-specific READ spans. Never join across a denied fragment."""
    fields = sorted(SEMANTIC_CONTEXT_FIELDS.intersection(fields))
    if not fields:
        return {}
    candidates = defaultdict(list)
    segment = []
    tables = defaultdict(list)

    def submit(spans):
        if not spans or len(spans) > 5:
            return
        joined = " ".join(x[3] for x in spans)
        if len(joined) > 600 or not current(joined):
            return
        for field in fields:
            if field in _FIELD_CUE and _FIELD_CUE[field].search(joined):
                candidates[field].append(spans)

    def finish():
        for i, item in enumerate(segment):
            if not any(cue.search(item[3]) for cue in _FIELD_CUE.values()):
                continue
            # Larger current context first; shrink if the original source does
            # not prove the exact sequence. Every selected line keeps its locator.
            for lo, hi in (
                (max(0, i - 2), min(len(segment), i + 3)),
                (max(0, i - 1), min(len(segment), i + 2)),
                (i, i + 1),
            ):
                spans = segment[lo:hi]
                if len(" ".join(x[3] for x in spans)) <= 600 and current(
                    " ".join(x[3] for x in spans)
                ):
                    submit(spans)
                    break
        segment.clear()

    ended = False
    for block in blocks:
        if ended:
            break
        text = block["text"]
        if block.get("cell"):
            finish()
            if (table := _CELL.fullmatch(block["pointer"])) and text.strip():
                tables[table["table"]].append(block)
            continue
        for line in re.finditer(r"[^\r\n]+", text):
            t = line[0].strip()
            if not t:
                continue
            if _SIGNOFF.search(t):
                ended = True
                break
            follows_label = bool(
                segment
                and (
                    (
                        _SAFE_TABLE_LABEL.search(segment[-1][3])
                        and segment[-1][3].endswith((":", "："))
                        and _VALUE_AFTER_LABEL.fullmatch(t)
                    )
                    or (
                        _CONTINUATION.fullmatch(t)
                        and any(_FIELD_CUE[f].search(segment[-1][3]) for f in fields)
                    )
                )
            )
            if (
                len(t) > 180
                or _FENCE.search(t)
                or _SCOPE_CHANGE.search(t)
                or not allow(t)
                or not (
                    follows_label
                    or _RELATED.search(t)
                    or any(cue.search(t) for _, cue in _FAMILIES)
                )
            ):
                finish()
                continue
            start = line.start() + len(line[0]) - len(line[0].lstrip())
            segment.append((block, start, start + len(t), t))
    finish()
    for cells in tables.values():
        grid = defaultdict(list)
        for b in cells:
            cell = b["cell"]
            row, col = cell["start_row_offset_idx"], cell["start_col_offset_idx"]
            if (
                type(row) is int
                and type(col) is int
                and (cell["end_row_offset_idx"], cell["end_col_offset_idx"]) == (row + 1, col + 1)
            ):
                grid[row, col].append(b)
        for (row, col), blocks_at_cell in grid.items():
            if len(blocks_at_cell) != 1:
                continue
            label = blocks_at_cell[0]
            if not _SAFE_TABLE_LABEL.search(label["text"]):
                continue
            for adjacent in ((row, col + 1), (row + 1, col)):
                if len(pair := grid.get(adjacent, ())) == 1:
                    cells_to_send = (label, pair[0])
                    if all(
                        0 < len(b["text"]) <= 180
                        and "\n" not in b["text"]
                        and not _FENCE.search(b["text"])
                        and allow(b["text"])
                        for b in cells_to_send
                    ):
                        submit([(b, 0, len(b["text"]), b["text"]) for b in cells_to_send])
    result = {}
    for field, windows in candidates.items():
        # Keep windows intact rather than truncating a qualifier off the end.
        unique = {}
        for window in windows:
            keys = tuple((b["pointer"], a, z) for b, a, z, _ in window)
            unique.setdefault(keys, window)
        # One complete window per gap is sufficient for a first request.
        result[field] = max(
            unique.values(),
            key=lambda w: (sum(bool(_FIELD_CUE[field].search(x[3])) for x in w), len(w)),
        )
    return result

"""Visible email HTML and current inline-image selection."""

import re
from html.parser import HTMLParser
from urllib.parse import unquote

BLOCKS = set(
    "address article aside br div dl dt dd h1 h2 h3 h4 h5 h6 hr li ol p pre section table tr ul blockquote".split()
)
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
OMIT = {"head", "script", "style", "template", "noscript", "iframe", "svg", "object"}


class BodyText(HTMLParser):
    """Show inert text from HTML and keep recognizable quoted content out of fields."""

    def __init__(self, scope_quotes=True):
        super().__init__(convert_charrefs=True)
        self.scope_quotes = scope_quotes
        self.stack = []
        self.parts = []
        self.cells = {}
        self.table_number = 0

    def add(self, value):
        if not any(frame[1] for frame in self.stack):
            self.parts.append((any(frame[2] for frame in self.stack), value, self.cell_pointer()))

    def cell_pointer(self):
        for tag, _, _, layout in reversed(self.stack):
            if tag in {"td", "th", "table"}:
                return layout["pointer"] if tag != "table" and layout else None
        return None

    def table_layout(self, tag, attributes):
        if tag == "table":
            self.table_number += 1
            return {"number": self.table_number, "next_row": 0, "occupied": {}, "valid": True}
        if tag not in {"tr", "td", "th"}:
            return None
        for ancestor, _, _, layout in reversed(self.stack):
            if tag == "tr" and ancestor == "table":
                row = layout["next_row"]
                layout["next_row"] += 1
                return {"table": layout, "row": row, "column": 0}
            if tag != "tr" and ancestor == "tr" and layout:
                break
            if ancestor == "table":
                return None
        else:
            return None
        table, row = layout["table"], layout["row"]
        span = [attributes.get(name, "1") for name in ("rowspan", "colspan")]
        if not all(re.fullmatch(r"[1-9][0-9]{0,2}", size or "") for size in span):
            table["valid"] = False
            return None
        height, width = map(int, span)
        occupied, column = table["occupied"], layout["column"]
        while any(occupied.get(i, 0) > row for i in range(column, column + width)):
            column += 1
        for i in range(column, column + width):
            occupied[i] = row + height
        layout["column"] = column + width
        pointer = f"/tables/{table['number']}/data/table_cells/{len(self.cells)}/text"
        cell = {
            "table": table,
            "pointer": pointer,
            "row": row,
            "row_end": row + height,
            "col": column,
            "col_end": column + width,
            "header": tag == "th",
        }
        self.cells[pointer] = cell
        return cell

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in BLOCKS or tag in {"td", "th"}:
            self.add("\n")
        if tag not in VOID:
            style = (attributes.get("style") or "").lower().replace(" ", "")
            hidden = (
                "hidden" in attributes or "display:none" in style or "visibility:hidden" in style
            )
            marker = " ".join(attributes.get(k) or "" for k in ("class", "id"))
            quote = self.scope_quotes and (
                tag == "blockquote"
                or bool(
                    re.search(
                        r"(?:gmail_quote|yahoo_quoted|moz-cite-prefix|divrplyfwdmsg|mail-editor-reference)",
                        marker,
                        re.I,
                    )
                )
            )
            self.stack.append(
                (tag, hidden or tag in OMIT, quote, self.table_layout(tag, attributes))
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if tag in BLOCKS or tag in {"td", "th"}:
                    self.add("\n")
                del self.stack[index:]
                if tag in BLOCKS or tag in {"td", "th"}:
                    self.add("\n")
                break

    def handle_data(self, data):
        self.add(re.sub(r"\s+", " ", data))


def quote_boundary(text):
    """Apply the shared header rule to visible lines without joining a lone From to its value."""
    from .main_channel import _quoted_offset

    blocks, offset = [], 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            blocks.append(
                {"text": line.rstrip("\r\n"), "offset": offset, "inline_group": "visible-lines"}
            )
        offset += len(line)
    for index, block in enumerate(blocks):
        if (cut := _quoted_offset(blocks, index)) is not None:
            return block["offset"] + cut
    return len(text)


def current_inline_cids(html):
    """Identify inline images in the current visible body, not forwarded content."""

    class Images(BodyText):
        def __init__(self):
            super().__init__()
            self.markers = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            source = attributes.get("src", "") if tag == "img" else ""
            style = (attributes.get("style") or "").lower().replace(" ", "")
            hidden = (
                "hidden" in attributes or "display:none" in style or "visibility:hidden" in style
            )
            if not hidden and source.lower().startswith("cid:"):
                marker = f"\n\ue000inline-{len(self.markers)}\ue001\n"
                self.markers[marker] = unquote(source[4:]).strip("<>")
                self.add(marker)
            super().handle_starttag(tag, attrs)

    parser = Images()
    parser.feed(html)
    parser.close()
    current = "".join(value if not quoted else "\n" for quoted, value, _ in parser.parts)
    boundary, offset, result = quote_boundary(current), 0, []
    for quoted, value, _ in parser.parts:
        if not quoted and value in parser.markers and offset < boundary:
            result.append(parser.markers[value])
        offset += 1 if quoted else len(value)
    return result

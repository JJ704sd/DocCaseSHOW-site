"""Build an inert HTML view from a stored email body and its verified CID images."""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import unquote

TAGS = set(
    "a address b blockquote br caption center cite code col colgroup dd del div dl dt em font h1 h2 h3 h4 h5 h6 hr i ins li ol p pre s small span strike strong sub sup table tbody td tfoot th thead tr u ul".split()
)
VOID = {"br", "col", "hr"}
DROP_CONTENT = {
    "audio",
    "iframe",
    "math",
    "noscript",
    "object",
    "script",
    "svg",
    "template",
    "textarea",
    "title",
    "video",
}
CSS_PROPERTIES = set(
    """
    background-color border border-bottom border-collapse border-color border-left border-radius
    border-right border-spacing border-style border-top border-width box-sizing clear color
    display float font font-family font-size font-style font-variant font-weight height
    letter-spacing line-height list-style-type margin margin-bottom margin-left margin-right
    margin-top max-height max-width min-height min-width overflow-wrap padding padding-bottom
    padding-left padding-right padding-top table-layout text-align text-decoration
    text-decoration-color text-decoration-style text-decoration-thickness text-indent
    text-transform vertical-align white-space width word-break word-spacing
""".split()
)
CSS_VALUE = re.compile(r"[\w\s#.,%()'\"/!+\-]+")
SAFE_FUNCTIONS = {"rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp"}
SELECTOR = re.compile(r"[\w.\-#\s>,+~*]+")
STYLE = """
html{color-scheme:light;background:white}
body{margin:12px;font:14px/1.5 'Segoe UI','Microsoft YaHei UI',sans-serif;overflow-wrap:anywhere}
img{max-width:100%;object-fit:contain}
.doccase-preview-blocked{display:inline-block;border:1px dashed #9ba9bb;padding:2px 6px;color:#526880;font-size:12px}
"""


def safe_declarations(text):
    declarations = []
    for part in text.split(";"):
        key, sep, value = part.partition(":")
        key, value = key.strip().lower(), value.strip()
        if sep and key in CSS_PROPERTIES and CSS_VALUE.fullmatch(value):
            functions = re.findall(r"([\w-]+)\s*\(", value)
            if all(name.lower() in SAFE_FUNCTIONS for name in functions):
                declarations.append(f"{key}:{value}")
    return ";".join(declarations)


def safe_stylesheet(text):
    # Keep simple top-level rules; omit imports, nested at-rules and unsupported selectors.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    depth, start, body_start, nested = 0, 0, 0, False
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                body_start, nested = index, False
            else:
                nested = True
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                selector = text[start:body_start].strip()
                if not nested and SELECTOR.fullmatch(selector):
                    if body := safe_declarations(text[body_start + 1 : index]):
                        rules.append(f"{selector}{{{body}}}")
                start = index + 1
    return "\n".join(rules)


def safe_attributes(attributes):
    output = []
    for name, value in attributes.items():
        if value is None:
            continue
        if name == "style":
            value = safe_declarations(value)
        elif name in {
            "width",
            "height",
            "colspan",
            "rowspan",
            "span",
            "cellpadding",
            "cellspacing",
            "border",
            "start",
        }:
            if not re.fullmatch(r"\d{1,5}%?", value):
                continue
        elif name not in {
            "align",
            "alt",
            "bgcolor",
            "class",
            "color",
            "dir",
            "face",
            "id",
            "lang",
            "size",
            "title",
            "valign",
        }:
            continue
        if value:
            output.append(f' {name}="{escape(value, quote=True)}"')
    return "".join(output)


class MailPreview(HTMLParser):
    def __init__(self, resolve_inline, image_budget):
        super().__init__(convert_charrefs=True)
        self.resolve_inline = resolve_inline
        self.image_budget = image_budget
        self.parts = []
        self.styles = []
        self.stylesheet = None
        self.body_attrs = ""
        self.in_head = False
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.append(tag)
            return
        if tag in DROP_CONTENT:
            self.blocked.append(tag)
        elif tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head = False
            self.body_attrs = safe_attributes(dict(attrs))
        elif tag == "style":
            self.stylesheet = []
        elif not self.in_head and tag == "img":
            self.add_image(dict(attrs))
        elif not self.in_head and tag in TAGS:
            self.parts.append(f"<{tag}{safe_attributes(dict(attrs))}>")

    def handle_endtag(self, tag):
        if self.blocked:
            if self.blocked[-1] == tag:
                self.blocked.pop()
            return
        if tag == "style" and self.stylesheet is not None:
            self.styles.append(safe_stylesheet("".join(self.stylesheet)))
            self.stylesheet = None
        elif tag == "head":
            self.in_head = False
        elif not self.in_head and tag in TAGS - VOID:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, text):
        if self.blocked:
            return
        if self.stylesheet is not None:
            self.stylesheet.append(text)
        elif not self.in_head:
            self.parts.append(escape(text, quote=False))

    def add_image(self, attrs):
        src = (attrs.get("src") or "").strip()
        if src[:4].lower() == "cid:":
            local = self.resolve_inline(unquote(src[4:]).strip().strip("<>"))
            if local and len(local) <= self.image_budget:
                self.image_budget -= len(local)
                self.parts.append(f'<img src="{local}"{safe_attributes(attrs)}>')
                return
            message = "内嵌图片超出预览范围" if local else "内嵌图片不可用"
        else:
            message = "外链图片未加载" if src else "图片未提供来源"
        self.parts.append(f'<span class="doccase-preview-blocked">[{message}]</span>')


def render_original_html(text, resolve_inline, *, image_budget):
    parser = MailPreview(resolve_inline, image_budget)
    parser.feed(text)
    parser.close()
    styles = "\n".join([STYLE, *parser.styles])
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{styles}</style></head>'
        f"<body{parser.body_attrs}>{''.join(parser.parts)}</body></html>"
    ).encode("utf-8")

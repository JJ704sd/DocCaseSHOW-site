"""Synthetic EML cases that use the ordinary local upload and parsing route."""

import base64
from email.message import EmailMessage
from email.policy import SMTP
from functools import lru_cache
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

CASES = (
    {
        "id": "current-body",
        "title": "当前正文与历史引用",
        "formats": ["HTML 正文", "TXT"],
        "description": "本次询盘改用 FCA，旧报价和附件备注仍可查阅。",
        "emails": 1,
        "check": "核对正文的 FCA 和 2.75 m³；历史引用的 FOB、3.20 m³ 不进入当前字段。",
    },
    {
        "id": "reply-conflict",
        "title": "业务组回复与冲突",
        "formats": ["邮件线程"],
        "description": "首询、重复回复和更改包装数的回复组成一组。",
        "emails": 3,
        "check": "包装件数 4 和 5 保留冲突；相同贸易条款作为重复印证，三封邮件不相加。",
    },
    {
        "id": "pdf-packing",
        "title": "PDF 装箱货行",
        "formats": ["PDF"],
        "description": "同一张装箱单同时有货行、单独的总计行和不同重量列。",
        "emails": 1,
        "check": "在附件「解析内容」定位 2 箱和 3 箱两条货行；总计 5 箱单独核对。",
    },
    {
        "id": "xlsx-packing",
        "title": "Excel 箱数与商品件数",
        "formats": ["XLSX"],
        "description": "同表分列包装箱数与商品数量，并保留原单元格。",
        "emails": 1,
        "check": "打开 Packing List，核对 C5 的 5 箱与 D5 的 100 件；两者是不同口径。",
    },
    {
        "id": "image-uncertain",
        "title": "图片附件与待确认重量",
        "formats": ["PNG", "HTML 正文"],
        "description": "正文只写 Weight 和 Pcs，附件是另列明细的图片。",
        "emails": 1,
        "check": "正文 285 kg 是计费重，245 kg 和 100 Pcs 可在「原文待归类」回查；图片 OCR 单独看。",
    },
)
_CASE_IDS = {case["id"] for case in CASES}


def _mail(subject, body, tag, *, day=10, reply_to=None):
    email = EmailMessage(policy=SMTP)
    email["From"] = "Sample Dispatch <dispatch@example.test>"
    email["To"] = "Demo Ops <ops@example.test>"
    email["Subject"] = subject
    email["Date"] = f"{day} Nov 2026 09:00:00 +0800"
    email["Message-ID"] = f"<{tag}@doccase.example.test>"
    if reply_to:
        email["In-Reply-To"] = email["References"] = f"<{reply_to}@doccase.example.test>"
    email.set_content(body, subtype="html")
    return email


def _packing_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    name = NameObject
    font = DictionaryObject(
        {
            name("/Type"): name("/Font"),
            name("/Subtype"): name("/Type1"),
            name("/BaseFont"): name("/Helvetica"),
        }
    )
    page[name("/Resources")] = DictionaryObject(
        {name("/Font"): DictionaryObject({name("/F1"): font})}
    )
    columns = (44, 88, 255, 322, 414, 501, 568)
    ops = ["0.5 w", "BT /F1 16 Tf 45 740 Td (Packing List) Tj ET"]
    ops.extend(f"{x} 591 m {x} 707 l S" for x in columns)
    ops.extend(f"44 {y} m 568 {y} l S" for y in (591, 620, 649, 678, 707))
    rows = (
        ("Item", "Product Name", "Cartons", r"Gross Weight \(KG\)", r"Net Weight \(KG\)", "CBM"),
        ("1", "Sample A", "2", "17.5", "16", "0.05"),
        ("2", "Sample B", "3", "9", "8", "0.07"),
        ("Total", "", "5", "26.5", "24", "0.12"),
    )
    for y, row in zip((688, 659, 630, 601), rows, strict=True):
        ops.extend(
            f"BT /F1 9 Tf {x + 4} {y} Td ({text}) Tj ET"
            for x, text in zip(columns[:-1], row, strict=True)
        )
    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("ascii"))
    page.replace_contents(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _packing_xlsx():
    # The sample is a small static workbook. It needs no writer dependency at runtime.
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    records = (
        ("Packing List",),
        ("Item", "Description", "CTNS", "Total Qty (PCS)", "Total GW (KG)", "Total CBM"),
        (1, "Sample bolts", 2, 40, 17.5, 0.05),
        (2, "Sample brackets", 3, 60, 9, 0.07),
        ("TOTAL:", "", 5, 100, 26.5, 0.12),
    )
    rows = []
    for n, record in enumerate(records, 1):
        cells = []
        for column, value in zip("ABCDEF", record, strict=False):
            payload = (
                f't="inlineStr"><is><t>{escape(value)}</t></is>'
                if isinstance(value, str)
                else f"><v>{value}</v>"
            )
            cells.append(f'<c r="{column}{n}" {payload}</c>')
        rows.append(f'<row r="{n}">{"".join(cells)}</row>')
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{mime}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{mime}.worksheet+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" '
            f'Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="Packing List" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(rows)}</sheetData></worksheet>',
        )
    return out.getvalue()


def _packing_png():
    image = Image.new("RGB", (860, 350), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    for index, line in enumerate(
        ("PACKING NOTE", "Total packages: 2 cartons", "Total gross weight: 118 kg")
    ):
        draw.text((45, 42 + index * 94), line, fill="black", font=font)
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _mails(case_id):
    if case_id == "current-body":
        mail = _mail(
            "Updated inquiry: FCA to SIN",
            "<p>Trade terms: FCA</p><p>Destination: SIN</p>"
            "<p>General cargo</p><p>Total volume: 2.75 CBM</p>"
            "<blockquote><p>On Monday wrote:</p><p>Trade terms: FOB</p>"
            "<p>Total volume: 3.20 CBM</p></blockquote>",
            case_id,
        )
        mail.add_attachment("Loading note: ask for a collection slot.", filename="loading-note.txt")
    elif case_id == "reply-conflict":
        root = "reply-conflict-first"
        for index, packages in enumerate((4, 4, 5), 1):
            yield (
                f"0{index}-inquiry.eml",
                _mail(
                    ("Re: " if index > 1 else "") + "Freight inquiry to SIN",
                    f"<p>Trade terms: FCA</p><p>Destination: SIN</p><p>Total packages: {packages} packages</p>",
                    root if index == 1 else f"reply-conflict-{index}",
                    day=9 + index,
                    reply_to=root if index > 1 else None,
                ),
            )
        return
    elif case_id == "pdf-packing":
        mail = _mail(
            "Inquiry with PDF packing list",
            "<p>Destination: SIN</p><p>See the packing list.</p>",
            case_id,
        )
        mail.add_attachment(_packing_pdf(), "application", "pdf", filename="packing-list.pdf")
    elif case_id == "xlsx-packing":
        mail = _mail(
            "Inquiry with Excel packing list",
            "<p>Destination: SIN</p><p>See the packing workbook.</p>",
            case_id,
        )
        mail.add_attachment(
            _packing_xlsx(),
            "application",
            "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="packing-list.xlsx",
        )
    else:
        mail = _mail(
            "Weight basis to confirm",
            "<p>Cargo details:</p><p>Pcs: 100</p><p>Weight: 245 kg</p>"
            "<p>Chargeable weight: 285 kg</p>",
            case_id,
        )
        mail.add_attachment(_packing_png(), "image", "png", filename="packing-note.png")
    yield f"{case_id}.eml", mail


@lru_cache(maxsize=len(CASES))
def case_files(case_id):
    if case_id not in _CASE_IDS:
        raise LookupError(case_id)
    files = []
    for filename, mail in _mails(case_id):
        if mail.is_multipart():
            mail.set_boundary(f"DocCase-{case_id}")
        files.append(
            {"filename": filename, "content": base64.b64encode(mail.as_bytes()).decode("ascii")}
        )
    return files

import hashlib
import json
import sys
import time
from email.message import EmailMessage
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import p02_local_read as p02
import pytest
from local_attachment_markdown import attachment_view, digest, encode, workbook_read
from PIL import Image

from local_web import Workbench, comparison_entry, excel_view, make_handler, text_view


def test_group_review_keeps_email_and_attachment_scope(tmp_path):
    app = Workbench(tmp_path, Path("unused"), tmp_path)

    def add(label, text, pdf_note):
        msg = EmailMessage()
        msg["Subject"] = label
        msg.set_content(text)
        msg.add_attachment(pdf_note, filename="packing-note.txt")
        return app.submit(label + ".eml", msg.as_bytes())

    first = add(
        "current",
        "Terms: FCA\nDestination: SCL\nTotal gross weight: 100 kg",
        "Total gross weight: 30 kg",
    )
    reply = add(
        "reply", "Terms: FCA\nDestination: BOG\nOrigin: Shanghai", "Total gross weight: 30 kg"
    )
    extra = add("extra", "Origin: Shanghai", "Total gross weight: 70 kg")
    independent = app.submit("independent.txt", b"Total gross weight: 999 kg")
    app.pool.shutdown()

    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request_group(current, *ids):
            query = urlencode([("current", current), *(("email", key) for key in ids)])
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/group-review?{query}", timeout=5
            ) as response:
                return json.load(response)

        try:
            result = request_group(first, reply, first, extra)
            assert [mail["id"] for mail in result["emails"]] == [first, reply, extra]
            assert "totals" not in result
            fields = {f["name"]: f for f in result["body_fields"]}
            assert fields["trade_terms"]["status"] == "duplicate"
            assert fields["destination"]["status"] == "conflict"
            assert fields["origin"]["status"] == "supplement"
            assert fields["line_volume"]["status"] == "uncertain"
            assert fields["line_gross_weight"]["status"] == "current"
            assert [
                (f["email_id"], f["value"])
                for f in fields["destination"]["entries"]
                if f["sources"]
            ] == [(first, "SCL"), (reply, "BOG")]
            first_doc, reply_doc, extra_doc = [mail["attachments"][0] for mail in result["emails"]]
            assert reply_doc["duplicate_of"] == {"email_id": first, "material_id": first_doc["id"]}
            assert extra_doc["duplicate_of"] is None and extra_doc["relationship"] == "supplement"
            assert extra_doc["mime_path"] and any(
                f["value"] == 70 and f["sources"] for f in extra_doc["fields"]
            )
            for current, ids in (
                (first, (first, independent)),
                (first, (reply, extra)),
                (first, (first, first)),
            ):
                with pytest.raises(HTTPError) as error:
                    request_group(current, *ids)
                assert error.value.code == 400
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_eml_upload_keeps_current_fields_when_attachment_is_broken(tmp_path):
    message = EmailMessage()
    message["Subject"] = "Freight inquiry"
    message["From"] = "ops@example.test"
    message.set_content("Destination: alternate representation")
    message.add_alternative(
        "<p>Please quote the shipment from ABC to DEF with the details below.</p>"
        "<p>Terms: FOB</p><p>Pcs: 8</p><p>Weight: 160 KG</p><p>Measures: 0.70 CBM</p>"
        "<blockquote>Destination: Earlier booking</blockquote>"
        "<script>Destination: script text</script>",
        subtype="html",
    )
    message.add_attachment(
        b"invalid pdf", maintype="application", subtype="pdf", filename="bad.pdf"
    )
    message.add_attachment(
        b"Total gross weight: 140 kg", maintype="text", subtype="plain", filename="ok.txt"
    )
    app = Workbench(tmp_path, Path("unused"), tmp_path)
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/jobs"

        def fetch(url):
            with urlopen(url, timeout=5) as response:
                return json.load(response)

        try:
            req = Request(base, data=message.as_bytes(), headers={"X-DocCase-File": "inquiry.eml"})
            job_url = base + "/" + fetch(req)["id"]
            deadline = time.monotonic() + 5
            while (job := fetch(job_url))["state"] in {
                "queued",
                "running",
            } and time.monotonic() < deadline:
                time.sleep(0.02)
            assert job["state"] == "partial"
            assert job["metadata"][0]["subject"] == "Freight inquiry"
            body, broken, working = job["materials"]
            assert (broken["filename"], broken["state"]) == ("bad.pdf", "failed")
            result = fetch(job_url + "/materials/" + body["id"])
            fields = {field["name"]: field for field in result["fields"]}
            for name, value, excerpt in (
                ("destination", "DEF", "shipment from ABC to DEF"),
                ("trade_terms", "FOB", "Terms: FOB"),
                ("line_volume", 0.70, "Measures: 0.70 CBM"),
            ):
                field = fields[name]
                assert (field["value"], field["status"]) == (value, "known")
                (source,) = field["sources"]
                assert (
                    excerpt == source["excerpt"] and excerpt in result["lines"][source["line"] - 1]
                )
            for name in ("origin", "ctns", "package_count", "line_gross_weight"):
                assert fields[name]["status"] == "uncertain"
            assert {source["excerpt"] for source in result["observations"]} == {
                "Pcs: 8",
                "Weight: 160 KG",
            }
            assert fields["chargeable_weight"]["status"] == "uncertain"
            assert "Earlier booking" in result["quoted"] and "script text" not in "\n".join(
                result["lines"]
            )
            result = fetch(job_url + "/materials/" + working["id"])
            assert (
                next(field for field in result["fields"] if field["name"] == "line_gross_weight")[
                    "value"
                ]
                == 140
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            app.pool.shutdown()


def test_excel_field_cites_label_and_value_cells():
    sheet = {
        "name": "Packing",
        "columns": ["A", "B"],
        "clipped": False,
        "rows": [
            {
                "number": 7,
                "cells": {
                    "A": {"value": "Total gross weight", "clipped": False},
                    "B": {"value": "420 kg", "clipped": False},
                },
            }
        ],
    }
    result = excel_view([sheet])
    gross = next(field for field in result["fields"] if field["name"] == "line_gross_weight")
    assert (gross["value"], gross["unit"], gross["status"]) == (420, "kg", "known")
    (evidence,) = gross["sources"]
    assert (evidence["sheet"], evidence["cell"], evidence["end_cell"]) == ("Packing", "A7", "B7")


def test_eml_original_html_preserves_layout_and_uses_only_same_mail_cid_images(tmp_path):
    email = EmailMessage()
    email["Subject"] = "[部分内容未导出] Preview fixture"
    email.set_content("Destination: Plain alternate")
    email.add_alternative(
        "<html><head><style>.right {text-align:right}</style></head><body>"
        '<table><tr><td class="right" style="color:#234567;background-image:url(https://remote.test/t)">'
        "Destination: DEF �</td></tr></table>"
        '<img src="cid:within@fixture" onerror="window.attack=1" width="12" alt="Retained">'
        '<img src="https://remote.test/pixel" alt="Remote">'
        '<img src="cid:missing@fixture" alt="Missing">'
        "<blockquote>Quoted original</blockquote><script>window.attack=2</script>"
        '<a href="https://remote.test/leave">inert label</a><iframe src="https://remote.test/f"></iframe>'
        "</body></html>",
        subtype="html",
    )
    png = BytesIO()
    Image.new("RGB", (2, 2), "white").save(png, format="PNG")
    email.get_body(preferencelist=("html",)).add_related(
        png.getvalue(),
        maintype="image",
        subtype="png",
        cid="<within@fixture>",
        filename="local.png",
    )
    app = Workbench(tmp_path, Path("unused"), tmp_path)
    key = app.submit("preview.eml", email.as_bytes())
    app.pool.shutdown()
    body = next(m for m in app.summary(key)["materials"] if m["kind"] == "body")
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/jobs/{key}/materials/{body['id']}"
        try:
            with urlopen(url + "/original-html", timeout=5) as response:
                markup = response.read().decode("utf-8")
                csp = response.headers["Content-Security-Policy"]
                assert "sandbox" in csp and "default-src 'none'" in csp and "img-src data:" in csp
            assert (
                "<table>" in markup and 'class="right"' in markup and "text-align:right" in markup
            )
            assert (
                "<img " in markup
                and 'src="data:image/png;base64,' in markup
                and 'width="12"' in markup
            )
            assert (
                "Quoted original" in markup
                and "外链图片未加载" in markup
                and "内嵌图片不可用" in markup
            )
            assert all(
                token not in markup
                for token in ("remote.test", "window.attack", "onerror", "<iframe", "href=")
            )
            with urlopen(url, timeout=5) as response:
                detail = json.load(response)
            assert detail["mail_preview"] == {
                "export_incomplete": True,
                "replacement_characters": 1,
            }
            assert detail["quoted"] == "Quoted original"
            assert (
                next(f for f in detail["fields"] if f["name"] == "destination")["value"] == "DEF �"
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("total_label", ["TOTAL:", "Total总计"])
def test_excel_total_uses_headers_and_does_not_treat_carton_numbers_as_counts(total_label):
    sheet = {
        "name": "Packing",
        "columns": ["A", "B", "C", "D", "E"],
        "clipped": False,
        "rows": [
            {
                "number": n,
                "cells": {
                    col: {"value": value, "clipped": False} for col, value in entries.items()
                },
            }
            for n, entries in [
                (7, {"A": "Items", "B": "CTN NO.", "C": "G.W./KG", "D": "N.W./KG", "E": "CBM"}),
                (8, {"B": "2", "C": "12", "D": "10", "E": "0.70"}),
                (9, {"A": total_label, "B": "2", "C": "12KGS", "D": "10KGS", "E": "0.70"}),
            ]
        ],
    }
    fields = {f["name"]: f for f in excel_view([sheet])["fields"]}
    assert (fields["line_gross_weight"]["value"], fields["line_gross_weight"]["unit"]) == (12, "kg")
    assert (fields["line_net_weight"]["value"], fields["line_net_weight"]["unit"]) == (10, "kg")
    assert (fields["line_volume"]["value"], fields["line_volume"]["unit"]) == (0.70, "CBM")
    assert {s["cell"] for s in fields["line_volume"]["sources"]} == {"A9", "E7", "E9"}
    assert fields["ctns"]["status"] == "uncertain"


def test_xlsx_packing_table_keeps_cartons_product_quantity_and_row_sources_separate():
    rows = {
        3: {"A": "Packing list"},
        13: {
            "A": "STYLE NO.",
            "B": "Q'TY/CTN",
            "C": "CTNS",
            "D": "TOTAL Q'TY",
            "E": "TOTAL GW (KG)",
            "F": "TOTAL CBM",
            "G": "CARTON WEIGHT (KG)",
        },
        14: {"A": "AT-001", "B": "100", "C": "2", "D": "200", "E": "50", "F": "0.6", "G": "25"},
        15: {"A": "AT-002", "B": "50", "C": "1", "D": "50", "E": "20", "F": "0.2", "G": "20"},
        17: {"A": "TOTAL", "C": "3", "D": "250", "E": "70", "F": "0.8"},
    }
    sheet = {
        "name": "Packing",
        "columns": list("ABCDEFG"),
        "clipped": False,
        "rows": [
            {
                "number": n,
                "cells": {col: {"value": val, "clipped": False} for col, val in row.items()},
            }
            for n, row in rows.items()
        ],
    }
    data = excel_view([sheet])
    fields = {f["name"]: f for f in data["fields"]}
    assert (fields["ctns"]["value"], fields["ctns"]["unit"]) == (3, "carton")
    assert (fields["item_quantity"]["value"], fields["item_quantity"]["unit"]) == (250, "piece")
    assert fields["package_count"]["status"] == "uncertain"
    assert fields["line_gross_weight"]["value"] == 70
    assert {s["cell"] for s in fields["ctns"]["sources"]} == {"A17", "C13", "C17"}
    packing = data["sheets"][0]["packing"]
    assert (packing["header_row"], packing["total_row"], packing["detail_rows"]) == (
        13,
        17,
        [14, 15],
    )
    assert packing["columns"]["item_quantity"] == "D"


def test_body_html_table_uses_adjacent_cells_and_retains_both_sources():
    result = text_view(
        b"<table><tr><td>ORIGIN</td><td colspan='3'>Example Airport</td></tr>"
        b"<tr><td>CARGO WILL BE READY DATE:</td><td colspan='2'>16/12/2026</td>"
        b"<td>Deliver before cutoff</td></tr><tr><td>TOTAL CBM:</td><td>5</td></tr></table>"
        b"<blockquote><table><tr><td>ORIGIN</td><td>Old Airport</td></tr></table></blockquote>",
        "html",
        True,
    )
    fields = {f["name"]: f for f in result["fields"]}
    assert fields["origin"]["value"] == "Example Airport"
    assert fields["cargo_ready_time"]["value"] == "16/12/2026"
    assert (fields["line_volume"]["value"], fields["line_volume"]["unit"]) == (5, "CBM")
    sources = fields["cargo_ready_time"]["sources"]
    assert {s["excerpt"] for s in sources} == {"CARGO WILL BE READY DATE:", "16/12/2026"}
    cells = {c["pointer"]: c["text"] for table in result["tables"] for c in table["cells"]}
    assert all(cells[s["pointer"]] == s["excerpt"] for s in sources)


def test_body_html_table_reads_values_after_a_standalone_total_label():
    result = text_view(
        b"<table><tr><th>CBM</th><th>Gross Weight</th></tr>"
        b"<tr><td>0.25 CBM</td><td>10 kg</td></tr><tr><td>0.75 CBM</td><td>30 kg</td></tr>"
        b"<tr><td></td><td>TOTAL</td></tr><tr><td>1.00 CBM</td><td>40 kg</td></tr></table>",
        "html",
        True,
    )
    fields = {f["name"]: f for f in result["fields"]}
    gross = fields["line_gross_weight"]
    assert (gross["value"], gross["unit"]) == (40, "kg")
    assert {"40 kg", "Gross Weight", "TOTAL"} <= {s["excerpt"] for s in gross["sources"]}
    cells = {c["pointer"]: c["text"] for table in result["tables"] for c in table["cells"]}
    assert all(cells[s["pointer"]] == s["excerpt"] for s in gross["sources"])
    assert fields["line_volume"]["value"] == 1


@pytest.mark.parametrize("fmt", ["text", "html"])
def test_current_body_uses_inquiry_context_and_keeps_line_sources(fmt):
    lines = ["Please quote under EXW terms", "G. Weight: 125 kgs", "Measure: 0.52 CBM"]
    source = "\n".join(lines) if fmt == "text" else "".join(f"<p>{line}</p>" for line in lines)
    result = text_view(source.encode(), fmt, True)
    fields = {field["name"]: field for field in result["fields"]}
    for name, value, excerpt in zip(
        ("trade_terms", "line_gross_weight", "line_volume"), ("EXW", 125, 0.52), lines, strict=True
    ):
        field = fields[name]
        assert (field["value"], field["status"]) == (value, "known")
        (evidence,) = field["sources"]
        assert evidence["excerpt"] == result["lines"][evidence["line"] - 1] == excerpt
    assert fields["chargeable_weight"]["status"] == "uncertain"


@pytest.mark.parametrize("complete_headers", [False, True])
def test_split_forwarded_headers_scope_current_lines_and_tables(complete_headers):
    headers = "<p>From:<br>Previous sender</p>"
    if complete_headers:
        headers += "<p>Sent:<br>Yesterday<br>To:<br>Previous recipient</p>"
    source = (
        "<p>Terms: FOB</p>" + headers + "<p>Terms: EXW</p>"
        "<table><tr><td>TOTAL CBM:</td><td>3.25</td></tr></table>"
    )
    result = text_view(source.encode(), "html", True)
    fields = {field["name"]: field for field in result["fields"]}
    if complete_headers:
        assert (fields["trade_terms"]["value"], fields["trade_terms"]["status"]) == ("FOB", "known")
        assert fields["line_volume"]["status"] == "uncertain"
        assert "Terms: EXW" in result["quoted"] and not result["tables"]
    else:
        assert fields["trade_terms"]["status"] == "conflict"
        assert fields["line_volume"]["value"] == 3.25


def test_html_attachment_reads_totals_with_cell_sources():
    result = text_view(
        "<table><tr><th>Description</th><th>CTN NO.</th><th>N.W. (KG)</th>"
        "<th>G.W. (KG)</th><th>CBM</th></tr>"
        "<tr><td>Total总计</td><td>1-4</td><td>12</td><td>15</td><td>0.3</td></tr></table>".encode(),
        "html",
        False,
    )
    fields = {field["name"]: field for field in result["fields"]}
    cells = {cell["pointer"]: cell["text"] for table in result["tables"] for cell in table["cells"]}
    for name, value in (("line_net_weight", 12), ("line_gross_weight", 15), ("line_volume", 0.3)):
        field = fields[name]
        assert (field["value"], field["status"]) == (value, "known")
        assert all(cells[source["pointer"]] == source["excerpt"] for source in field["sources"])
    assert fields["ctns"]["status"] == "uncertain"


@pytest.mark.parametrize("scope", ["all", "body", "attachments"])
def test_email_comparison_uses_only_selected_same_mail_fields_and_keeps_unknowns(tmp_path, scope):
    email = EmailMessage()
    email["Subject"] = "Comparison fixture"
    email.set_content(
        "Total packages: 11 packages\nTotal gross weight: 510 kg\nTotal volume: 1.39 CBM"
    )
    email.add_attachment(
        "Total packages: 1 package\nTotal gross weight: 140 kg\nTotal volume: 0.27 CBM",
        filename="first.txt",
    )
    email.add_attachment(
        "Total packages: 10 packages\nTotal gross weight: 280 kg\nTotal gross weight: 290 kg",
        filename="second.txt",
    )
    app = Workbench(tmp_path, Path("unused"), tmp_path)
    key = app.submit("mail.eml", email.as_bytes(), scope)
    other = app.submit("independent.txt", b"Total gross weight: 999 kg", scope)
    app.pool.shutdown()
    body, first, second = app.summary(key)["materials"]
    assert body["state"] == ("skipped" if scope == "attachments" else "ready")
    assert first["state"] == second["state"] == ("skipped" if scope == "body" else "ready")
    assert app.summary(other)["materials"][0]["state"] == "ready"
    result = app.comparison(key, [first["id"], second["id"]])
    assert result["body"]["id"] == body["id"]
    assert [item["id"] for item in result["attachments"]] == [first["id"], second["id"]]
    count, gross, volume = (
        result["totals"][f] for f in ("package_count", "line_gross_weight", "line_volume")
    )
    assert count["comparison"] == ("match" if scope == "all" else "unresolved")
    assert count["selected_document_sum"] == (None if scope == "body" else 11)
    assert gross["selected_document_sum"] is None and gross["selected_status"] == (
        "uncertain" if scope == "body" else "conflict"
    )
    assert volume["selected_document_sum"] is None and volume["selected_status"] == "uncertain"
    if scope != "body":
        conflicting = result["attachments"][1]["fields"]["line_gross_weight"]
        assert conflicting["status"] == "conflict" and conflicting["value"] is None
        assert {s["excerpt"] for s in conflicting["sources"]} == {
            "Total gross weight: 280 kg",
            "Total gross weight: 290 kg",
        }
    with pytest.raises(ValueError, match="comparison_invalid_selection"):
        app.comparison(key, [body["id"]])
    with pytest.raises(ValueError, match="comparison_not_email"):
        app.comparison(other, ["0"])


def test_attachment_markdown_binds_fields_to_sheets_and_preserves_conflict(tmp_path):
    sheets = [
        {
            "name": name,
            "columns": ["A", "B"],
            "clipped": False,
            "rows": [
                {
                    "number": 5,
                    "cells": {
                        "A": {"value": "Total gross weight", "clipped": False},
                        "B": {"value": weight, "clipped": False},
                    },
                }
            ],
        }
        for name, weight in (("First", "120 kg"), ("Second", "140 kg"))
    ]
    detail = excel_view(sheets)
    parts = workbook_read(detail)
    record = {
        "material_id": "M1",
        "content": {"sha256": "0" * 64},
        "filename": "sample.xlsx",
        "kind": "attachment",
        "format": "xlsx",
        "state": "ready",
    }

    def exporter(read):
        cells = [cell for part in read["parts"] for cell in part["cells"]]
        md = ""
        fragments = []
        for number, cell in enumerate(cells, 1):
            start = len(md)
            md += cell["text"]
            fragments.append(
                {
                    "evidence_id": f"E{number}",
                    "location": cell["location"],
                    "markdown_start": start,
                    "markdown_end": len(md),
                }
            )
            md += "\n"
        return {
            "markdown": md,
            "index": {
                "material_id": "M1",
                "read_sha256": digest(encode(read)),
                "fragments": fragments,
                "limitations": [],
            },
        }

    view = attachment_view(
        record,
        detail,
        "docling",
        tmp_path,
        exporter,
        parts=parts,
        reader="openpyxl",
        profile="local-xlsx-packing",
    )
    fields = {f["name"]: f for f in view["structured"]["fields"]}
    gross = fields["line_gross_weight"]
    assert gross["status"] == "conflict" and gross["value"] is None
    assert [
        (s["sheet"], s["cell"], s["end_cell"], s["markdown_evidence"]) for s in gross["sources"]
    ] == [
        ("First", "A5", "B5", ["E1", "E2"]),
        ("Second", "A5", "B5", ["E3", "E4"]),
    ]
    assert fields["line_volume"]["status"] == "uncertain"

    def wrong_cell(read):
        result = exporter(read)
        result["index"]["fragments"][1]["location"] = {"sheet": "Second", "cell": "B5"}
        return result

    with pytest.raises(ValueError, match="source mismatch"):
        attachment_view(
            record,
            detail,
            "docling",
            tmp_path,
            wrong_cell,
            parts=parts,
            reader="openpyxl",
            profile="local-xlsx-packing",
        )


def test_upload_opt_in_chain_surfaces_verified_agent_field_in_regular_view(tmp_path, monkeypatch):
    from openpyxl import Workbook

    sentence = "Number of packages has reduced from 947nos to 683nos."
    cargo_lines = (sentence, "Cargo details:", "Weight: 245 kg", "Chargeable weight: 285 kg")
    message = EmailMessage()
    message.set_content("".join(f"<p>{line}</p>" for line in cargo_lines), subtype="html")
    versions = p02.configurations()["docling", "html"]["versions"]
    conversions = []

    def write(path, value):
        path.write_bytes(value)
        return hashlib.sha256(value).hexdigest()

    def worker(request, **_options):
        if request["operation"] == "probe":
            return versions
        if request["operation"] == "convert":
            conversions.append(request["operation"])
            path = Path(request["json_path"])
            if request["format"] == "xlsx":
                cells = [
                    {
                        "text": text,
                        "start_row_offset_idx": row,
                        "end_row_offset_idx": row + 1,
                        "start_col_offset_idx": col,
                        "end_col_offset_idx": col + 1,
                        "column_header": row == 0,
                    }
                    for row, values in enumerate(
                        (
                            ("Description", "CARTONS", "Total Qty (PCS)", "Total CBM"),
                            ("Goods", "50", "300", "1.25"),
                            ("TOTAL:", "50", "300", "1.25"),
                        )
                    )
                    for col, text in enumerate(values)
                ]
                document = {
                    "texts": [],
                    "groups": [
                        {"self_ref": "#/groups/0", "label": "sheet", "name": "Packing List"}
                    ],
                    "tables": [
                        {
                            "parent": {"$ref": "#/groups/0"},
                            "prov": [
                                {
                                    "page_no": 1,
                                    "bbox": {
                                        "l": 0,
                                        "t": 0,
                                        "r": 4,
                                        "b": 3,
                                        "coord_origin": "TOPLEFT",
                                    },
                                }
                            ],
                            "data": {"num_rows": 3, "num_cols": 4, "table_cells": cells},
                        }
                    ],
                }
            else:
                document = {"texts": [{"text": line} for line in cargo_lines], "tables": []}
            sha = write(path, json.dumps(document).encode())
            return {
                "conversion_status": "success",
                "versions": versions,
                "errors": [],
                "json_path": str(path),
                "json_sha256": sha,
            }
        assert request["operation"] == "export_markdown"
        if request["source_kind"] == "attachment":
            return {"state": "failure", "reason": "no_markdown_table_mapping"}
        root = Path(request["output_dir"])
        root.mkdir(parents=True)
        md, index = root / "content.md", root / "index.json"
        md_sha = write(md, sentence.encode())
        binding = {k: request[k] for k in ("read_sha256", "material_id", "execution_id")}
        ix_sha = write(
            index,
            json.dumps(
                {
                    **binding,
                    "markdown_sha256": md_sha,
                    "limitations": [],
                    "fragments": [
                        {
                            "evidence_id": "E1",
                            "source_role": "current",
                            "read_pointer": "/texts/0/text",
                            "read_start": 0,
                            "read_end": len(sentence),
                            "markdown_start": 0,
                            "markdown_end": len(sentence),
                            "provenance": [],
                        }
                    ],
                }
            ).encode(),
        )
        return {
            "state": "success",
            "markdown_ref": str(md),
            "markdown_sha256": md_sha,
            "index_ref": str(index),
            "index_sha256": ix_sha,
        }

    calls = []
    material_calls = []

    def adapter(request, *, timeout_seconds):
        calls.append(request)
        return {
            "requirement_id": request["requirement_id"],
            "candidate": {"value": 683, "evidence_ids": ["E1"]},
        }

    def material_adapter(request, *, timeout_seconds):
        material_calls.append(request)
        if request["source_kind"] == "attachment":
            raise AssertionError("Known attachment totals do not trigger a repeat request")
        assert request["source_kind"] == "current_body"
        assert [item["field_name"] for item in request["requirements"]] == ["package_count"]
        assert [item["text"] for item in request["evidence"]] == [sentence]
        evidence = next(item for item in request["evidence"] if item["text"] == sentence)
        return {
            "candidates": [
                {
                    "requirement_id": "REQ-PACKAGE_COUNT",
                    "value": 683,
                    "evidence_ids": [evidence["evidence_id"]],
                },
            ]
        }

    adapter.extract_material = material_adapter
    monkeypatch.setattr(p02, "worker", worker)
    app = Workbench(tmp_path, Path(sys.executable), tmp_path, extraction_agent=adapter)
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"

        def get(path, **kwargs):
            with urlopen(Request(root + path, **kwargs), timeout=5) as response:
                return json.load(response)

        try:
            for route, expected in (("docling", None), ("deepseek", 683)):
                key = get(
                    "/api/jobs",
                    data=message.as_bytes(),
                    headers={"X-DocCase-File": "trial.eml", "X-DocCase-Route": route},
                )["id"]
                url = f"/api/jobs/{key}"
                deadline = time.monotonic() + 4
                while (
                    get(url)["state"]
                    in {
                        "queued",
                        "running",
                    }
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                data = get(f"{url}/materials/0")
                field = next(f for f in data["fields"] if f["name"] == "package_count")
                assert field["value"] == expected
                chain = data["chain"]
                assert (
                    chain["markdown"]["text"] == sentence
                    and chain["read"]["reader"] == "DoclingHtml"
                )
                assert chain["agent"]["adapter_requested"] == (route == "deepseek")
                if expected:
                    assert (
                        field["via"] == "agent"
                        and field["sources"][0]["pointer"] == "/texts/0/text"
                    )
                    assert chain["agent"]["events"][0]["outcome"] == "accepted"
            assert len(calls) == 1 and calls[0]["evidence"][0]["representation"] == "markdown"
            group = []
            for label in ("current", "related"):
                if "Subject" in message:
                    message.replace_header("Subject", label)
                else:
                    message["Subject"] = label
                if label == "related":
                    book = Workbook()
                    book.active.title = "Packing List"
                    for row in (
                        ("Description", "CARTONS", "Total Qty (PCS)", "Total CBM"),
                        ("Goods", 50, 300, 1.25),
                        ("TOTAL:", 50, 300, 1.25),
                    ):
                        book.active.append(row)
                    file = BytesIO()
                    book.save(file)
                    message.add_attachment(
                        file.getvalue(),
                        filename="Packing List.xlsx",
                        maintype="application",
                        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                group.append(
                    get(
                        "/api/jobs",
                        data=message.as_bytes(),
                        headers={
                            "X-DocCase-File": f"{label}.eml",
                            "X-DocCase-Route": "deepseek-group",
                        },
                    )["id"]
                )
            deadline = time.monotonic() + 4
            while any(get(f"/api/jobs/{key}")["state"] in {"queued", "running"} for key in group):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            query = urlencode([("current", group[0]), *(("email", k) for k in group)])
            before = get(f"/api/group-review?{query}")
            assert before["agent"]["state"] == "ready"
            quantity = next(f for f in before["group_fields"] if f["name"] == "item_quantity")
            assert quantity["status"] == "supplement"
            [quantity_entry] = quantity["attachment_entries"]
            assert (quantity_entry["value"], quantity_entry["unit"]) == (300, "piece")
            assert {s["cell"] for s in quantity_entry["sources"]} == {"A3", "C1", "C3"}
            assert [item["email_id"] for item in before["body_observations"]] == group
            for item in before["body_observations"]:
                [source] = item["sources"]
                assert source["excerpt"] == "Weight: 245 kg" and source["line"] > 1
            assert len(calls) == 1
            before_conversions = len(conversions)
            assert (
                get(
                    f"/api/group-extraction?{query}",
                    method="POST",
                    headers={"X-DocCase-Group": "deepseek"},
                )["state"]
                == "running"
            )
            deadline = time.monotonic() + 5
            while (after := get(f"/api/group-review?{query}"))["agent"]["state"] == "running":
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert after["agent"]["state"] == "complete", after["agent"]
            quantity = next(f for f in after["group_fields"] if f["name"] == "item_quantity")
            assert quantity["attachment_entries"][0]["value"] == 300
            assert {s["cell"] for s in quantity["attachment_entries"][0]["sources"]} == {
                "A3",
                "C1",
                "C3",
            }
            assert after["agent"]["accepted"] == 1 and after["agent"]["requests"] == 1
            assert after["agent"]["corroborated"] == 0 and after["agent"]["rejected"] == 0
            assert "route_comparison" not in before
            delta = after["route_comparison"]
            assert delta["scope"] == "same_read"
            assert (delta["group_value_changes"], delta["other_changes"]) == (1, 0)
            [change] = delta["changes"]
            assert change["name"] == "package_count"
            assert (change["rules"]["value"], change["combined"]["value"]) == (None, 683)
            audit = [event for item in after["agent"]["validation"] for event in item["events"]]
            receipt = next(
                item for item in after["agent"]["validation"] if item.get("requested_fields")
            )
            assert receipt["email_id"] == group[0] and receipt["requested_fields"] == [
                "package_count"
            ]
            assert all("proposed_value" not in e for e in audit)
            gross = next(f for f in after["group_fields"] if f["name"] == "shipment_gross_weight")
            assert gross["value"] is None and gross["status"] == "uncertain"
            assert (
                len(calls) == 1
                and len(material_calls) == 1
                and len(conversions) == before_conversions
            )
            field = next(f for f in after["group_fields"] if f["name"] == "package_count")
            assert field["value"] == 683 and field["body_entries"][0]["via"] == "agent"
            cartons = next(f for f in after["group_fields"] if f["name"] == "ctns")
            assert cartons["status"] == "supplement" and cartons["value"] is None
            assert [item["value"] for item in cartons["attachment_entries"]] == [50]
            original = get(f"/api/jobs/{group[0]}/materials/0")
            assert (
                next(f for f in original["fields"] if f["name"] == "package_count")["value"] is None
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            app.pool.shutdown()
            app.group_pool.shutdown()


def test_comparison_uses_scoped_shipment_gross_instead_of_other_weight_fields():
    def weight(name, value, status="known"):
        return {
            "name": name,
            "value": value,
            "unit": "kg",
            "status": status,
            "sources": [{"excerpt": f"{value} kg"}] if value is not None else [],
        }

    row = {"kind": "body"}
    fields = [
        weight("line_gross_weight", 150),
        weight("unit_gross_weight", 18),
        weight("shipment_chargeable_weight", 500),
    ]
    known = comparison_entry(row, {"fields": [*fields, weight("shipment_gross_weight", 420)]})
    assert known["fields"]["line_gross_weight"]["value"] == 420
    unknown = comparison_entry(
        row, {"fields": [*fields, weight("shipment_gross_weight", None, "uncertain")]}
    )
    assert unknown["fields"]["line_gross_weight"]["status"] == "uncertain"
    assert unknown["fields"]["line_gross_weight"]["value"] is None


def test_xlsx_pipeline_locates_only_cells_matching_the_selected_workbook(tmp_path):
    from local_web_pipeline import pipeline_view

    document = {
        "groups": [{"self_ref": "#/groups/0", "label": "sheet", "name": "Packing"}],
        "tables": [
            {
                "parent": {"$ref": "#/groups/0"},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 2,
                            "t": 2,
                            "r": 4,
                            "b": 5,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                ],
                "data": {
                    "num_rows": 3,
                    "num_cols": 2,
                    "table_cells": [
                        {
                            "text": "5",
                            "start_row_offset_idx": 2,
                            "end_row_offset_idx": 3,
                            "start_col_offset_idx": 1,
                            "end_col_offset_idx": 2,
                        },
                        {
                            "text": "7",
                            "start_row_offset_idx": 1,
                            "end_row_offset_idx": 2,
                            "start_col_offset_idx": 0,
                            "end_col_offset_idx": 1,
                        },
                    ],
                },
            }
        ],
    }
    doc_bytes = json.dumps(document).encode()
    doc_path = tmp_path / "read.json"
    doc_path.write_bytes(doc_bytes)
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    result_ref = {"ref": str(result_path), "sha256": hashlib.sha256(b"{}").hexdigest()}
    (tmp_path / "progress.json").write_text(
        json.dumps({"result_ref": result_ref}), encoding="utf-8"
    )
    execution = {
        "execution_id": "read-1",
        "ref": str(doc_path),
        "artifact_hash": hashlib.sha256(doc_bytes).hexdigest(),
        "executed_profile": {
            "reader": "DoclingExcel",
            "requested_scope": {"sheet_names": ["Packing"]},
        },
    }
    read = {
        "inventory": [{"material_id": "m1", "execution_id": "read-1", "read_state": "success"}],
        "executions": [execution],
    }
    sources = [
        {
            "source": {
                "material_id": "m1",
                "kind": "attachment",
                "excerpt": text,
                "locator": {"json_pointer": f"/tables/0/data/table_cells/{i}/text", "page": 1},
            }
        }
        for i, text in enumerate(("5", "7"))
    ]
    response = {
        "status": "complete",
        "profile_id": "test",
        "fields": [
            {
                "field_name": "ctns",
                "value": 5,
                "unit": "carton",
                "status": "known",
                "issues": [],
                "sources": sources,
            },
        ],
    }
    record = {
        "material_id": "m1",
        "filename": "fixture.xlsx",
        "kind": "attachment",
        "format": "xlsx",
        "state": "ready",
        "content": {"sha256": "0" * 64},
    }
    sheet = {
        "name": "Packing",
        "clipped": False,
        "columns": ["A", "B", "C", "D"],
        "rows": [
            {"number": 4, "cells": {"C": {"value": "8", "clipped": False}}},
            {"number": 5, "cells": {"D": {"value": "5", "clipped": False}}},
        ],
    }

    def project(selected):
        result = pipeline_view(
            {"result_ref": result_ref},
            read,
            response,
            record,
            "deepseek",
            tmp_path,
            xlsx_sheet=selected,
        )
        return result["structured"]["fields"][0]["sources"]

    located, mismatch = project(sheet)
    assert (located["sheet"], located["cell"], located["page"]) == ("Packing", "D5", None)
    assert "sheet" not in mismatch and "cell" not in mismatch
    assert all("sheet" not in source for source in project({**sheet, "name": "Other"}))
    unbound = {
        "name": "item_quantity",
        "status": "known",
        "value": 7,
        "unit": "piece",
        "sources": [{"sheet": "Packing", "cell": "C4", "excerpt": "7"}],
    }
    result = pipeline_view(
        {"result_ref": result_ref},
        read,
        response,
        record,
        "deepseek",
        tmp_path,
        xlsx_sheet=sheet,
        xlsx_fields=[unbound],
    )
    assert not any(field["name"] == "item_quantity" for field in result["structured"]["fields"])


@pytest.mark.parametrize(
    ("lines", "count", "volume"),
    [
        (
            ("Cargo Details:", "General Cargo", "1 Package:", "Dims.: 20*30*40cm", "GW:52KG"),
            1,
            0.024,
        ),
        (("Cargo Details:", "1 Package:", "Dims.: 200*300*400mm"), 1, 0.024),
        (("Cargo Details:", "1 Package:", "Dims.: 0.2*0.3*0.4m"), 1, 0.024),
        (("Cargo Details:", "2 Packages:", "Dims.: 20*30*40cm"), 2, None),
        (
            (
                "Current reply",
                "----- Original -----",
                "Cargo Details:",
                "1 Package:",
                "Dims.: 20*30*40cm",
            ),
            None,
            None,
        ),
        (("Cargo Details:", "1 Package:", "Dims.: 20*30*40cm", "2 Packages:"), None, None),
    ],
)
def test_group_review_exposes_single_package_volume_as_an_estimate(
    tmp_path, monkeypatch, lines, count, volume
):
    mail = EmailMessage()
    mail.set_content("".join(f"<p>{line}</p>" for line in lines), subtype="html")
    versions = p02.configurations()["docling", "html"]["versions"]

    def worker(request, **_options):
        if request["operation"] == "probe":
            return versions
        if request["operation"] == "export_markdown":
            return {"state": "failure", "reason": "unavailable"}
        raw = json.dumps({"texts": [{"text": line} for line in lines], "tables": []}).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    monkeypatch.setattr(p02, "worker", worker)
    app = Workbench(tmp_path, Path(sys.executable), tmp_path)
    key = app.submit("sample.eml", mail.as_bytes(), route="docling")
    app.pool.shutdown()
    try:
        result = app.group_review(key, [key])
        if count is None:
            assert result["body_measurements"] == []
            return
        [measurement] = result["body_measurements"]
        assert measurement["package"]["label"].lower() in {"package", "packages"}
        assert measurement["package"]["count"] == count
        estimate = measurement["volume_estimate"]
        if volume is None:
            assert estimate is None
        else:
            assert (estimate["value"], estimate["unit"], estimate["status"]) == (
                volume,
                "CBM",
                "estimated",
            )
            assert {s["excerpt"] for s in estimate["sources"]} == {
                "1 Package:",
                next(x for x in lines if x.startswith("Dims")),
            }
            assert all(s.get("pointer") for s in estimate["sources"])
        assert (
            next(f for f in result["group_fields"] if f["name"] == "line_volume")["value"] is None
        )
        assert (
            next(f for f in result["group_fields"] if f["name"] == "package_type")["value"] is None
        )
    finally:
        app.group_pool.shutdown()


@pytest.mark.parametrize(
    ("quantity_header", "unit_header", "total", "expected"),
    [
        ("数量", "SET", "120 SET", (120, "set")),
        ("价格", "SET", "120 SET", None),
        ("数量", "SET", "120 PCS", None),
    ],
)
def test_excel_total_keeps_explicit_set_units(
    tmp_path, quantity_header, unit_header, total, expected
):
    sheet = {
        "name": "Packing",
        "columns": list("ABCDEF"),
        "clipped": False,
        "rows": [
            {
                "number": row,
                "cells": {col: {"value": value, "clipped": False} for col, value in cells.items()},
            }
            for row, cells in [
                (2, {"F": quantity_header}),
                (3, {"D": "CTN NO.", "F": unit_header}),
                (4, {"D": "1", "F": "120"}),
                (6, {"C": "TOTAL:", "D": "3", "F": total}),
            ]
        ],
    }
    fields = {field["name"]: field for field in excel_view([sheet])["fields"]}
    if expected is None:
        assert "item_quantity" not in fields
    else:
        field = fields["item_quantity"]
        assert (field["value"], field["unit"]) == expected
        assert {source["cell"] for source in field["sources"]} == {"C6", "F2", "F3", "F6"}
    assert fields["ctns"]["status"] == "uncertain"


def test_group_review_uses_verified_current_inline_signature_read(tmp_path):
    app = Workbench(tmp_path, Path("unused"), tmp_path)
    job_id, image_id, cid = "mail-one", "mat-signature", "signature@example"
    body = b'<p>Thanks</p><img src="cid:signature@example">'
    body_path = tmp_path / "body.html"
    body_path.write_bytes(body)
    body_ref = {"path": body_path.name, "sha256": hashlib.sha256(body).hexdigest()}
    image_hash = "a" * 64

    def line(text, x, y):
        return {
            "text": text,
            "location": {"line": 1, "box": [[x, y], [x + 80, y], [x + 80, y + 10], [x, y + 10]]},
        }

    lines = [
        line("Sample Person", 100, 10),
        line("Example", 10, 20),
        line("sample@example.invalid", 100, 40),
        line("+1 202 555 0191", 100, 60),
        line("+1 202 555 0192", 100, 80),
    ]
    for i, value in enumerate(lines, 1):
        value["location"]["line"] = i
    read = {
        "material_id": image_id,
        "input_sha256": image_hash,
        "reader": "RapidOCR",
        "parts": [{"lines": lines}],
    }
    read_bytes = encode(read)
    folder = tmp_path / job_id / f"attachment-{image_id}"
    folder.mkdir(parents=True)
    (folder / "read.json").write_bytes(read_bytes)

    def chain(mid, digest_value):
        return {
            "preprocessing": {"material_id": mid},
            "read": {"state": "success", "sha256": digest_value},
        }

    app.jobs[job_id] = {
        "id": job_id,
        "is_email": True,
        "filename": "sample.eml",
        "state": "ready",
        "reason": None,
        "parse_scope": "all",
        "route": "docling",
        "email_sha256": "b" * 64,
        "metadata": [{"email_ref_id": job_id, "from_addr": "sample@example.invalid"}],
        "materials": [
            {"id": "0", "kind": "body", "format": "html", "sha256": body_ref["sha256"]},
            {"id": "1", "kind": "inline", "format": "png", "sha256": image_hash},
        ],
        "details": {
            "0": {"fields": [], "chain": chain("body", "c" * 64)},
            "1": {"fields": [], "chain": chain(image_id, digest(read_bytes))},
        },
        "origins": {},
        "replays": {},
        "html_previews": {"0": (tmp_path, body_ref)},
        "inline_images": {cid: ({"sha256": image_hash}, "png")},
    }
    result = app.group_review(job_id, [job_id], confirmed_first_email_id=job_id)
    direct = {f["name"]: f for f in result["structured_result"]["first_inquiry"]["direct_fields"]}
    assert direct["contact_name"]["value"] == "Sample Person"
    assert direct["customer_company"]["value"] == "Example"
    assert direct["signature_email"]["value"] == "sample@example.invalid"
    assert direct["contact_name"]["sources"][0]["material_id"] == "1"
    assert direct["contact_phone"]["value"] == "+1 202 555 0191；+1 202 555 0192"
    assert direct["contact_phone"]["status"] == "known"
    assert len(direct["contact_phone"]["sources"]) == 2
    unconfirmed = app.group_review(job_id, [job_id])["structured_result"]["first_inquiry"]
    assert all(f["value"] is None for f in unconfirmed["direct_fields"])
    quoted = b'<p>Thanks</p><blockquote><img src="cid:signature@example"></blockquote>'
    body_path.write_bytes(quoted)
    app.jobs[job_id]["html_previews"]["0"] = (
        tmp_path,
        {"path": body_path.name, "sha256": digest(quoted)},
    )
    old = app.group_review(job_id, [job_id], confirmed_first_email_id=job_id)
    old_fields = {f["name"]: f for f in old["structured_result"]["first_inquiry"]["direct_fields"]}
    assert old_fields["signature_email"]["value"] is None
    app.pool.shutdown()
    app.group_pool.shutdown()


def test_signature_images_respect_hidden_and_quoted_html():
    from local_web import current_inline_cids

    html = (
        '<p>Hello</p><img src="cid:current">'
        '<img hidden src="cid:hidden">'
        '<blockquote><img src="cid:history"></blockquote>'
    )
    assert current_inline_cids(html) == ["current"]


def test_xlsx_row_volume_uses_the_source_cell_display_precision(tmp_path):
    from local_web_pipeline import pipeline_view

    values = (("Item", "Item Name", "CARTONS", "CUBIC (CBM)"), ("1", "Widget", "30", "1.23456"))
    cells = [
        {
            "text": text,
            "start_row_offset_idx": y,
            "end_row_offset_idx": y + 1,
            "start_col_offset_idx": x,
            "end_col_offset_idx": x + 1,
        }
        for y, row in enumerate(values)
        for x, text in enumerate(row)
    ]
    document = {
        "groups": [{"self_ref": "#/groups/0", "label": "sheet", "name": "Packing"}],
        "tables": [
            {
                "parent": {"$ref": "#/groups/0"},
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 0, "t": 0, "r": 4, "b": 2, "coord_origin": "TOPLEFT"},
                    }
                ],
                "data": {"num_rows": 2, "num_cols": 4, "table_cells": cells},
            }
        ],
    }
    doc_bytes = json.dumps(document).encode()
    doc_path = tmp_path / "read.json"
    doc_path.write_bytes(doc_bytes)
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    result_ref = {"ref": str(result_path), "sha256": hashlib.sha256(b"{}").hexdigest()}
    (tmp_path / "progress.json").write_text(
        json.dumps({"result_ref": result_ref}), encoding="utf-8"
    )
    execution = {
        "execution_id": "e",
        "ref": str(doc_path),
        "artifact_hash": hashlib.sha256(doc_bytes).hexdigest(),
        "executed_profile": {
            "reader": "DoclingExcel",
            "requested_scope": {"sheet_names": ["Packing"]},
        },
        "result": {
            "excel_evidence": {
                "selected_sheet": "Packing",
                "processed_sheet_names": ["Packing"],
                "docling_table_ranges": [{"table_ref": "#/tables/0", "sheet_name": "Packing"}],
            }
        },
    }
    read = {
        "inventory": [{"material_id": "m", "execution_id": "e", "read_state": "success"}],
        "executions": [execution],
    }
    sheet = {
        "name": "Packing",
        "columns": list("ABCD"),
        "clipped": False,
        "rows": [
            {
                "number": y + 1,
                "cells": {
                    col: {
                        "value": text,
                        "clipped": False,
                        **({"number_format": "0.000_ "} if col == "D" and y == 1 else {}),
                    }
                    for col, text in zip("ABCD", row, strict=True)
                },
            }
            for y, row in enumerate(values)
        ],
    }
    record = {
        "material_id": "m",
        "filename": "packing.xlsx",
        "kind": "attachment",
        "format": "xlsx",
        "state": "ready",
        "content": {"sha256": "0" * 64},
    }
    response = {"status": "complete", "profile_id": "test", "fields": []}
    result = pipeline_view(
        {"result_ref": result_ref},
        read,
        response,
        record,
        "deepseek-group",
        tmp_path,
        xlsx_sheet=sheet,
    )
    volume = result["structured"]["packing_rows"][0]["fields"]["line_volume"]
    assert volume["value"] == 1.235
    assert volume["sources"][0]["excerpt"] == "1.23456"
    assert (volume["sources"][0]["sheet"], volume["sources"][0]["cell"]) == ("Packing", "D2")
    assert "number_format" not in excel_view([sheet])["sheets"][0]["rows"][1]["cells"]["D"]
    sheet["rows"][1]["cells"]["D"]["number_format"] = "General"
    unchanged = pipeline_view(
        {"result_ref": result_ref},
        read,
        response,
        record,
        "deepseek-group",
        tmp_path,
        xlsx_sheet=sheet,
    )["structured"]["packing_rows"][0]["fields"]["line_volume"]
    assert unchanged["value"] == 1.23456

"""Test the Docling serialization boundary using the installed Docling runtime."""

import json
import os
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
DOCLING_PYTHON = Path(os.environ.get("DOCCASE_DOCLING_PYTHON", ""))
pytestmark = pytest.mark.skipif(
    not os.environ.get("DOCCASE_DOCLING_PYTHON") or not DOCLING_PYTHON.is_file(),
    reason="set DOCCASE_DOCLING_PYTHON to run Docling integration tests",
)


def test_docling_fragment_preserves_change_sentence_and_read_pointer():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, DocItemLabel
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
text = "Number of packages has reduced from 947nos to 683nos. Volume: 1.89 CBM."
doc.add_text(label=DocItemLabel.TEXT, text=text)
markdown, index = export_document(doc, read_sha256="a" * 64, material_id="M1",
                                  execution_id="X1", source_kind="current_body", source_text=text)
print(json.dumps({"markdown": markdown, "index": index}, ensure_ascii=False))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert "from 947nos to 683nos" in result["markdown"]
    assert "1.89 CBM" in result["markdown"]
    segment = result["index"]["fragments"][0]
    assert segment["read_pointer"] == "/texts/0/text"
    assert segment["read_start"] == 0 and segment["read_end"] == len(result["markdown"])
    assert segment["source_role"] == "current"


def test_docling_merged_table_preserves_cell_identity_without_inventing_a_header():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, TableCell, TableData
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=[
    TableCell(start_row_offset_idx=0, end_row_offset_idx=2, start_col_offset_idx=0,
              end_col_offset_idx=1, row_span=2, text="41"),
    TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=1,
              end_col_offset_idx=2, text="1.55"),
    TableCell(start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=1,
              end_col_offset_idx=2, text=""),
]))
md, index = export_document(doc, read_sha256="b" * 64, material_id="M", execution_id="X",
                            source_kind="attachment")
print(json.dumps({"markdown": md, "index": index}))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert '<td rowspan="2">41</td>' in result["markdown"]
    assert "<th" not in result["markdown"]
    assert result["markdown"].count("41") == 1
    assert {x["read_pointer"] for x in result["index"]["fragments"]} == {
        "/tables/0/data/table_cells/0/text",
        "/tables/0/data/table_cells/1/text",
    }


@pytest.mark.parametrize(
    "header",
    [
        "From: sender Sent: yesterday To: receiver",
        "De: sender Enviado el: yesterday Para: receiver",
        "De: sender Enviada em: yesterday Para: receiver",
        "------------------ Original ------------------",
    ],
)
def test_duplicate_text_uses_read_order_and_explicit_quoted_header(header):
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, DocItemLabel
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
for text in ("Total 40 cartons", sys.argv[1], "Total 40 cartons"):
    doc.add_text(label=DocItemLabel.TEXT, text=text)
source = "<p>Total 40 cartons</p><p>" + sys.argv[1] + "</p><p>Total 40 cartons</p>"
_, idx = export_document(doc, read_sha256="c" * 64, material_id="M", execution_id="X",
                         source_kind="current_body", source_text=source)
print(json.dumps(idx))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program, header],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    roles = {x["read_pointer"]: x["source_role"] for x in json.loads(process.stdout)["fragments"]}
    assert roles["/texts/0/text"] == "current"
    assert roles["/texts/2/text"] == "quoted"


def test_read_text_not_exported_is_reported_separately_from_mapping():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, DocItemLabel
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
doc.add_text(label=DocItemLabel.TEXT, text="exported")
doc.add_text(label=DocItemLabel.TEXT, text="READ-only value")
doc.body.children.pop()
md, index = export_document(doc, read_sha256="9" * 64, material_id="M", execution_id="X",
                            source_kind="attachment")
print(json.dumps({"markdown":md,"index":index}))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert "READ-only value" not in result["markdown"]
    assert {"reason": "read_content_not_exported", "read_pointer": "/texts/1/text"} in result[
        "index"
    ]["limitations"]
    assert result["index"]["state"] == "partial_success"


@pytest.mark.parametrize(
    "hidden, expected", [("", False), ("123", True), ('<img src="cid:x"/>', True)]
)
def test_hidden_content_is_only_ignored_when_empty(hidden, expected):
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, DocItemLabel
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
doc.add_text(label=DocItemLabel.TEXT, text="Visible")
source = '<p>Visible</p><span style="display:none">' + sys.argv[1] + '</span>'
_, idx = export_document(doc, read_sha256="d" * 64, material_id="M", execution_id="X",
                         source_kind="current_body", source_text=source)
print(json.dumps(idx))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program, hidden],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    reasons = [x["reason"] for x in json.loads(process.stdout)["limitations"]]
    assert ("hidden_source_content_not_confirmed_in_read" in reasons) is expected


def test_simple_explicit_header_and_nested_numbered_list_keep_values_and_order():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, TableCell, TableData
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
group = doc.add_list_group()
first = doc.add_list_item(text="1.89 CBM", enumerated=True, marker="1.", parent=group)
nested = doc.add_list_group(parent=first)
doc.add_list_item(text="1,234.50 kg", enumerated=True, marker="1.", parent=nested)
doc.add_list_item(text="second", enumerated=True, marker="2.", parent=group)
cells = [TableCell(start_row_offset_idx=r, end_row_offset_idx=r+1, start_col_offset_idx=c,
                   end_col_offset_idx=c+1, text=text, column_header=(r == 0))
         for r,c,text in [(0,0,"Gross"),(0,1,"Volume"),(1,0,"1,234.50 kg"),(1,1,"1.89 CBM")]]
doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=cells))
md, idx = export_document(doc, read_sha256="e" * 64, material_id="M", execution_id="X",
                         source_kind="attachment")
print(json.dumps({"markdown": md, "index": idx}))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    markdown = result["markdown"]
    assert markdown.index("1.89 CBM") < markdown.index("1,234.50 kg") < markdown.index("second")
    assert "1. " in markdown and "2. " in markdown and "| Gross | Volume |" in markdown
    assert (
        len([x for x in result["index"]["fragments"] if "/table_cells/" in x["read_pointer"]]) == 4
    )


def test_nested_table_is_an_explicit_read_reference_not_a_duplicated_value():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, TableCell, RichTableCell, TableData, RefItem
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
outer_cell = RichTableCell(start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=0,
                           end_col_offset_idx=1,text="",ref=RefItem(cref="#/tables/1"))
outer = doc.add_table(data=TableData(num_rows=1,num_cols=1,table_cells=[outer_cell]))
inner_cell = TableCell(start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=0,
                       end_col_offset_idx=1,text="17 kg")
doc.add_table(data=TableData(num_rows=1,num_cols=1,table_cells=[inner_cell]), parent=outer)
md, idx = export_document(doc, read_sha256="f" * 64, material_id="M", execution_id="X",
                         source_kind="attachment")
print(json.dumps({"markdown":md,"index":idx}))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert "nested structure in READ" in result["markdown"]
    assert result["markdown"].count("17 kg") <= 1
    assert any(
        x["reason"] == "nested_table_structure_requires_read"
        and x["read_pointer"] == "/tables/0/data/table_cells/0"
        for x in result["index"]["limitations"]
    )


def test_colspan_and_empty_cells_remain_in_structured_table():
    program = r"""
import json, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, TableCell, TableData
from p02_markdown_export import export_document
doc = DoclingDocument(name="test")
cells = [TableCell(start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=0,
                   end_col_offset_idx=2,col_span=2,text="total 3"),
         TableCell(start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=2,
                   end_col_offset_idx=3,text="")]
doc.add_table(data=TableData(num_rows=1,num_cols=3,table_cells=cells))
md, index = export_document(doc,read_sha256="a"*64,material_id="M",execution_id="X",source_kind="attachment")
print(json.dumps({"markdown":md,"index":index}))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert '<td colspan="2">total 3</td><td></td>' in result["markdown"]
    assert result["markdown"].count("total 3") == 1


def test_pdf_supplement_selects_one_part_per_page_and_retains_part_pointers(tmp_path):
    program = r"""
import hashlib, json, pathlib, sys
sys.path.insert(0, "examples")
from docling_core.types.doc import DoclingDocument, DocItemLabel, BoundingBox, Size, ProvenanceItem, TableCell, TableData
from p02_markdown_export import export_saved_read
root = pathlib.Path(sys.argv[1])
def doc(items, structure=False):
    d = DoclingDocument(name="test")
    for page in sorted({p for p, _ in items}): d.add_page(page, Size(width=100, height=100))
    for page, text in items:
        d.add_text(label=DocItemLabel.TEXT, text=text,
                   prov=ProvenanceItem(page_no=page, bbox=BoundingBox(l=0,t=0,r=30,b=20), charspan=(0,len(text))))
    if structure:
        cell = TableCell(start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=0,end_col_offset_idx=1,text="123")
        d.add_table(data=TableData(num_rows=1,num_cols=1,table_cells=[cell]),
                    prov=ProvenanceItem(page_no=1,bbox=BoundingBox(l=0,t=0,r=30,b=20),charspan=(0,0)))
    return d.export_to_dict()
parts = []
for name, kind, items in [("native", "native", [(1,"page one"),(2,"page two")]),
                          ("structure", "structure", [(1,"page one")])]:
    raw = json.dumps(doc(items, kind == 'structure')).encode(); path = root / (name + '.json'); path.write_bytes(raw)
    parts.append({"kind": kind, "pages": [1,2] if kind == 'native' else [1,1], "ref": str(path),
                  "sha256": hashlib.sha256(raw).hexdigest(), "document": json.loads(raw)})
bundle = {"kind":"docling_pdf_read_bundle", "expected_pages":[1,2], "parts":parts}
raw = json.dumps(bundle).encode(); path = root / 'bundle.json'; path.write_bytes(raw)
out = export_saved_read({"read_path":str(path), "read_sha256":hashlib.sha256(raw).hexdigest(),
                        "material_id":"M", "execution_id":"X", "source_kind":"attachment",
                        "output_dir":str(root/'out')})
print(json.dumps(out))
"""
    process = subprocess.run(
        [str(DOCLING_PYTHON), "-X", "utf8", "-c", program, str(tmp_path)],
        cwd=PROJECT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    markdown = Path(result["markdown_ref"]).read_text(encoding="utf-8")
    index = json.loads(Path(result["index_ref"]).read_text(encoding="utf-8"))
    assert markdown.count("page one") == 1 and markdown.count("page two") == 1
    assert {x["read_pointer"] for x in index["fragments"]} == {
        "/parts/0/document/texts/0/text",
        "/parts/0/document/texts/1/text",
    }
    assert index["structural_refs"][0]["read_pointer"] == "/parts/1/document/tables/0"
    assert index["structural_refs"][0]["cell_pointers"] == [
        "/parts/1/document/tables/0/data/table_cells/0/text"
    ]

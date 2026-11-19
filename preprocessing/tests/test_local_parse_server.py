"""Focused HTTP and registered-source checks for the local parsing entry."""

import hashlib
import importlib.util
import json
import threading
from copy import deepcopy
from email.message import EmailMessage
from http.server import HTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pytest
from PIL import Image

from doccase_preprocessing.main_channel import validate_material_handoff

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    spec = importlib.util.spec_from_file_location(
        "local_parse_server", EXAMPLES / "local_parse_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(app, tmp_path):
    parser = app.LocalParser(tmp_path, tmp_path / "runs", tmp_path / "python", tmp_path / "cache")
    http = HTTPServer(("127.0.0.1", 0), app.make_handler(parser))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", tmp_path / "runs"
    finally:
        http.shutdown()
        thread.join()
        http.server_close()


def post(base, path, value):
    request = Request(
        base + path,
        data=json.dumps(value).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_http_partial_returns_values_and_resolvable_missing_evidence(server):
    base, runs = server
    status, result = post(
        base, "/local/parse", {"email_id": "missing-synthetic", "profile_id": "partial-freight"}
    )
    assert status == 200
    assert result["status"] == "partial"
    fields = {field["field_name"]: field for field in result["fields"]}
    assert (fields["ctns"]["value"], fields["ctns"]["unit"]) == (3, "carton")
    assert (fields["line_gross_weight"]["value"], fields["line_gross_weight"]["unit"]) == (
        315,
        "kg",
    )
    assert fields["unit_gross_weight"]["value"] is None
    assert fields["line_volume"]["value"] is None
    issue = fields["line_volume"]["issues"][0]
    assert issue["code"] == "explicitly_referenced_material_unread"
    source = next(
        source for source in result["sources"] if source["source_id"] == issue["source_ids"][0]
    )
    assert "体积见附件M2-packing-list.pdf" in source["excerpt"]
    missing = next(
        material
        for material in result["materials"]
        if material["filename"] == "M2-packing-list.pdf"
    )
    assert (missing["read_state"], missing["reason"]) == ("blocked", "missing_content")
    saved = runs / result["run_id"] / "response.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == result
    snapshot = json.loads(Path(result["result_ref"]["ref"]).read_text(encoding="utf-8"))
    assert snapshot["intent"]["confirmed_by_caller"] is None


def test_http_rejects_unregistered_input_before_creating_run(server):
    base, runs = server
    for path, request, expected in [
        ("/local/parse", {"email_id": "../outside", "profile_id": "partial-freight"}, 400),
        ("/local/parse", {"email_id": "body-real", "profile_id": "partial-freight"}, 400),
        (
            "/local/parse",
            {"email_id": "missing-synthetic", "profile_id": "partial-freight", "path": "x"},
            400,
        ),
        ("/other", {"email_id": "missing-synthetic", "profile_id": "partial-freight"}, 404),
    ]:
        status, _ = post(base, path, request)
        assert status == expected
    assert not runs.exists()


def test_changed_registered_email_is_rejected(app, tmp_path):
    sample = {
        "archive": "test.zip",
        "member": "mail.eml",
        "email_sha256": "0" * 64,
    }
    with ZipFile(tmp_path / "test.zip", "w") as archive:
        archive.writestr("mail.eml", b"changed email")
    with pytest.raises(ValueError, match="differ from the fixed sample"):
        app._input(sample, tmp_path)


@pytest.mark.parametrize(
    "variant",
    [
        "current",
        "quoted",
        "hidden",
        "domain_mismatch",
        "tampered_read",
        "unrelated_text",
        "bad_coordinates",
        "known",
        "default",
    ],
)
def test_current_inline_company_is_returned_with_ocr_evidence(app, tmp_path, monkeypatch, variant):
    message = EmailMessage()
    message["From"] = (
        "Sample <sample@other.invalid>"
        if variant == "domain_mismatch"
        else "Sample <sample@example.invalid>"
    )
    image_tag = '<img src="cid:logo">'
    if variant == "quoted":
        image_tag = f"<blockquote>{image_tag}</blockquote>"
    elif variant == "hidden":
        image_tag = '<img hidden src="cid:logo">'
    paragraphs = ["Cargo details:", "12 cartons"]
    if variant == "known":
        paragraphs.append("Customer company: Prior Freight")
    message.set_content(
        "".join(f"<p>{line}</p>" for line in paragraphs) + image_tag, subtype="html"
    )
    image = BytesIO()
    Image.new("RGB", (200, 100), "white").save(image, format="PNG")
    message.add_related(image.getvalue(), maintype="image", subtype="png", cid="<logo>")
    monkeypatch.setattr(
        app.LocalParser,
        "validate_request",
        lambda *_: ("indexed", "fixture", app.WEIGHT_INQUIRY_PROFILE),
    )
    monkeypatch.setattr(
        app, "read_indexed_email", lambda *_: (message.as_bytes(), {"source": "fixture"})
    )
    versions = app.p02.configurations()["docling", "html"]["versions"]

    def worker(request, **_options):
        if request["operation"] == "probe":
            return versions
        raw = json.dumps({"texts": [{"text": line} for line in paragraphs], "tables": []}).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    calls = []

    def saved_ocr(_root, record, output, **_kwargs):
        calls.append(record["material_id"])
        text = "Example newsletter" if variant == "unrelated_text" else "Example Logistics Ltd"
        right = 250 if variant == "bad_coordinates" else 160
        read = {
            "material_id": record["material_id"],
            "input_sha256": record["content"]["sha256"],
            "reader": "RapidOCR",
            "parts": [
                {
                    "lines": [
                        {
                            "text": text,
                            "location": {
                                "line": 1,
                                "box": [[10, 10], [right, 10], [right, 30], [10, 30]],
                            },
                        }
                    ]
                }
            ],
        }
        path = output / "synthetic-ocr-read.json"
        raw = json.dumps(read).encode()
        path.write_bytes(raw)
        return {
            "ref": str(path),
            "sha256": "0" * 64 if variant == "tampered_read" else hashlib.sha256(raw).hexdigest(),
        }

    monkeypatch.setattr(app.p02, "worker", worker)
    monkeypatch.setattr(app, "_read_inline_image", saved_ocr, raising=False)
    (tmp_path / "cache").mkdir()
    parser = app.LocalParser(tmp_path, tmp_path / "runs", tmp_path / "python", tmp_path / "cache")
    parser.inline_image_ocr = variant != "default"
    result = parser.parse({"email_id": "fixture", "profile_id": app.WEIGHT_INQUIRY_PROFILE})
    company = next(f for f in result["fields"] if f["field_name"] == "customer_company")
    if variant != "current":
        assert (company["status"], company["value"]) == (
            ("known", "Prior Freight") if variant == "known" else ("uncertain", None)
        )
        assert len(calls) == (
            1
            if variant in {"domain_mismatch", "tampered_read", "unrelated_text", "bad_coordinates"}
            else 0
        )
        if variant == "tampered_read":
            assert result["inline_ocr"]["reads"][0]["reason"] == "read_hash_mismatch"
        return
    assert (company["status"], company["value"]) == ("known", "Example")
    source = company["sources"][0]["source"]
    assert source["kind"] == "signature" and source["excerpt"] == "Example"
    assert source["locator"]["json_pointer"] == "/parts/0/lines/0/text"
    assert source["locator"]["region"] == [10, 10, 160, 30]
    assert any(
        item["material_id"] == source["material_id"] for item in result["inline_ocr"]["reads"]
    )
    snapshot = app.read_validated_result(
        result["result_ref"], content_root=tmp_path / "runs" / result["run_id"] / "p01"
    )
    handoff = snapshot["material_handoff"]
    candidate = next(
        item for item in handoff["candidates"] if item["field_name"] == "customer_company"
    )
    assert (candidate["field"]["status"], candidate["field"]["value"]) == ("known", "Example")
    sources = {item["source_id"]: item for item in handoff["sources"]}
    image_source = sources[candidate["field"]["source_ids"][0]]
    assert image_source["kind"] == "signature" and image_source["locator"]["region"] == [
        10,
        10,
        160,
        30,
    ]
    assert result["inline_ocr"]["scope"] == "material_handoff"
    assert result["inline_ocr"]["base_result_ref"] != result["result_ref"]
    assert (handoff["result_revision"], handoff["base_revision"]) == (2, 1)
    for mutation in ("value", "region", "section"):
        changed = deepcopy(snapshot)
        if mutation == "value":
            next(
                item
                for item in changed["material_handoff"]["candidates"]
                if item["field_name"] == "customer_company"
            )["field"]["value"] = "Wrong Company"
        elif mutation == "region":
            next(
                item
                for item in changed["material_handoff"]["sources"]
                if item["source_id"] == image_source["source_id"]
            )["locator"]["region"] = [1, 1, 2, 2]
        else:
            next(
                item
                for item in changed["material_handoff"]["source_bindings"]
                if item["source_id"] == image_source["source_id"]
            )["section_evidence"]["cid"] = "wrong-cid"
        with pytest.raises(ValueError, match="Inline company evidence"):
            validate_material_handoff(
                changed,
                task_scope=changed.get("task_scope_source"),
                manifest_ref=changed["material_handoff"]["input_manifest_ref"],
                field_profile=changed["material_handoff"]["required_field_profile"],
                read_ref=changed["read_result_ref"],
                content_root=tmp_path / "runs" / result["run_id"] / "p01",
                field_contract_ref=changed["field_contract_source"],
            )


@pytest.mark.parametrize(
    ("volume_text", "mode"),
    [
        ("0,380 cbm", "success"),
        ("Total volume: 0.38 CBM", "no_gap"),
        ("0,380 cbm", "timeout"),
        ("0,380 cbm", "multi_shipment"),
    ],
)
def test_deepseek_material_route_requests_only_natural_gap_on_same_read(
    app,
    tmp_path,
    monkeypatch,
    volume_text,
    mode,
):
    paragraphs = ["Cargo details:", "12 cartons", "140,8 kg", volume_text]
    message = EmailMessage()
    message.set_content("".join(f"<p>{line}</p>" for line in paragraphs), subtype="html")
    monkeypatch.setattr(
        app.LocalParser,
        "validate_request",
        lambda *_: ("indexed", "fixture", app.WEIGHT_INQUIRY_PROFILE),
    )
    monkeypatch.setattr(
        app, "read_indexed_email", lambda *_: (message.as_bytes(), {"source": "fixture"})
    )
    monkeypatch.setattr(app.p02, "export_read_markdown", lambda *_a, **_k: {"state": "failure"})
    versions = app.p02.configurations()["docling", "html"]["versions"]
    conversions, sent = [], []

    def worker(request, **_options):
        if request["operation"] == "probe":
            return versions
        assert request["operation"] == "convert"
        conversions.append(request["format"])
        raw = json.dumps({"texts": [{"text": line} for line in paragraphs], "tables": []}).encode()
        path = Path(request["json_path"])
        path.write_bytes(raw)
        return {
            "conversion_status": "success",
            "errors": [],
            "versions": versions,
            "json_path": str(path),
            "json_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def agent(request, *, timeout_seconds):
        sent.append(request)
        if mode == "timeout":
            raise TimeoutError()
        evidence = [
            e["evidence_id"]
            for e in request["evidence"]
            if e["text"] in {"Cargo details:", "12 cartons", volume_text}
        ]
        unsent = next(eid for eid in ("E1", "E2", "E3", "E4") if eid not in evidence)
        return {
            "candidates": [
                {"requirement_id": "REQ-LINE_VOLUME", "value": 0.38, "evidence_ids": evidence},
                {"requirement_id": "REQ-CTNS", "value": 999, "evidence_ids": evidence},
                {"requirement_id": "REQ-LINE_VOLUME", "value": 0.9, "evidence_ids": [unsent]},
            ]
        }

    monkeypatch.setattr(app.p02, "worker", worker)
    if mode == "multi_shipment":
        monkeypatch.setattr(
            app,
            "body_objects",
            lambda *_a, **_k: {
                "shipments": [{"source_role": "current_body"}, {"source_role": "current_body"}],
            },
        )
    (tmp_path / "cache").mkdir()
    parser = app.LocalParser(
        tmp_path, tmp_path / "runs", tmp_path / "python", tmp_path / "cache", material_agent=agent
    )
    result = parser.parse({"email_id": "fixture", "profile_id": app.WEIGHT_INQUIRY_PROFILE})
    assert conversions == ["html"]
    assert len(sent) == int(mode in {"success", "timeout"})
    if mode in {"success", "timeout"}:
        assert [r["field_name"] for r in sent[0]["requirements"]] == ["line_volume"]
        assert [e["text"] for e in sent[0]["evidence"]] == [
            "Cargo details:",
            "12 cartons",
            volume_text,
        ]
    audit = json.loads(
        (tmp_path / "runs" / result["run_id"] / "agent-gap-selection.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["state"] == ("requested" if mode in {"success", "timeout"} else "not_sent")
    if mode == "success":
        assert audit["unsent_requirements"] == ["REQ-CTNS"]
        assert audit["unsent_evidence"] == ["REQ-LINE_VOLUME"]
    progress = json.loads(
        Path(result["result_ref"]["ref"]).with_name("progress.json").read_text(encoding="utf-8")
    )
    assert Path(result["result_ref"]["ref"]).parent.name == "main-channel"
    rule_progress = json.loads(
        (tmp_path / "runs" / result["run_id"] / "main-channel-rules" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["read"]["result_ref"]["sha256"] == rule_progress["read"]["result_ref"]["sha256"]
    events = json.loads(Path(progress["agent"]["ref"]).read_text(encoding="utf-8"))["events"]
    material_event = next(event for event in events if event.get("stage") == "material_agent")
    assert (
        material_event["outcome"]
        == {
            "success": "received",
            "no_gap": "skipped",
            "timeout": "fallback",
            "multi_shipment": "skipped",
        }[mode]
    )
    if mode in {"success", "timeout"}:
        assert material_event["requested_fields"] == 1 and material_event["read_fragments"] == 3
        if mode == "timeout":
            assert material_event["reason"] == "timeout"
    else:
        assert material_event["reason"] == "no_evidenced_gap"
    volume = next(f for f in result["fields"] if f["field_name"] == "line_volume")
    assert (volume["status"], volume["value"], volume["unit"]) == (
        ("uncertain", None, None)
        if mode in {"timeout", "multi_shipment"}
        else ("known", 0.38, "CBM")
    )
    cartons = next(f for f in result["fields"] if f["field_name"] == "ctns")
    assert (cartons["status"], cartons["value"]) == ("known", 12)


@pytest.mark.parametrize(
    ("profile_id", "anchor", "object_id"),
    [
        ("six-pdf-row", "#23", "pdf/packing-row-23"),
        ("six-pdf-row-22", "#01~#22", "pdf/packing-row-01-22"),
    ],
)
def test_six_field_profile_uses_one_declared_attachment_row(app, profile_id, anchor, object_id):
    target = {"material_id": "pdf"}
    profile = app._profile(profile_id, {"task_id": "task", "materials": [target]}, target)
    assert {entry["field_name"] for entry in profile["requirements"]} == {
        "ctns",
        "line_net_weight",
        "line_gross_weight",
        "unit_net_weight",
        "unit_gross_weight",
        "line_volume",
    }
    assert all(entry["pdf_row_anchor"] == anchor for entry in profile["requirements"])
    assert {entry["object_selector"] for entry in profile["requirements"]} == {object_id}

"""Input regressions through the public, local pipeline handoff."""

from email import policy
from email.message import EmailMessage

import pytest
from test_inputs import cid_bundle, eml

from doccase_preprocessing.pipeline import prepare


def test_invalid_html_handoff_keeps_the_usable_text_task(tmp_path):
    message = EmailMessage()
    message["Subject"] = "Broken HTML alternative"
    message.set_content("10 boxes. Regards, Buyer.")
    message.add_alternative("<p>10 boxes</p>", subtype="html")
    message.get_payload()[1].replace_header("Content-Type", "text/html; charset=x-unknown")
    result = prepare(eml(message.as_bytes(policy=policy.SMTP)), tmp_path)
    bodies = {m["declared_mime"]: m for m in result["materials"] if m["kind"] == "body"}
    assert result["state"] == "partial"
    assert bodies["text/plain"]["state"] == "ready"
    assert bodies["text/html"]["state"] == "blocked"
    assert bodies["text/html"]["issues"][0]["code"] == "unknown_charset"
    assert [t["route"] for t in result["parse_tasks"]] == ["text"]
    assert (tmp_path / bodies["text/plain"]["content"]["path"]).read_bytes().strip() == (
        b"10 boxes. Regards, Buyer."
    )


@pytest.mark.parametrize("input_kind", ["json", "eml"])
@pytest.mark.parametrize("present", [False, True])
def test_cid_handoff_reports_gap_without_blocking_the_body(tmp_path, input_kind, present):
    def no_fetch(*args):
        pytest.fail("CID reconciliation must not fetch resources")

    result = prepare(cid_bundle(input_kind, present), tmp_path, fetch_attachment=no_fetch)
    assert result["state"] == ("ready" if present else "partial")
    body = next(m for m in result["materials"] if m["declared_mime"] == "text/html")
    assert body["state"] == "ready" and body["parse_task_id"] is not None
    resources = [m for m in result["materials"] if m["cid"] == "packing@mail"]
    assert len(resources) == 1
    resource = resources[0]
    assert resource["state"] == ("ready" if present else "blocked")
    if not present:
        assert resource["selected_for_parsing"] and resource["content"] is None
        assert resource["issues"][0]["code"] == "missing_cid"
        assert resource["metadata"]["referenced_by_material_ids"] == [body["material_id"]]
        assert resource["parse_task_id"] is None

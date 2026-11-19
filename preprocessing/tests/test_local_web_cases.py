import base64
from email import policy
from email.parser import BytesParser
from io import BytesIO

from local_web_cases import CASES, case_files
from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader


def test_builtin_cases_contain_parseable_originals_and_a_linked_thread():
    parser = BytesParser(policy=policy.default)
    attachments = {}
    for case in CASES:
        messages = [
            parser.parsebytes(base64.b64decode(file["content"], validate=True))
            for file in case_files(case["id"])
        ]
        assert len(messages) == case["emails"]
        assert all(message.get_body(preferencelist=("html",)) for message in messages)
        for reply in messages[1:]:
            assert reply["In-Reply-To"] == messages[0]["Message-ID"]
        for message in messages:
            for part in message.iter_attachments():
                attachments[part.get_filename()] = part.get_payload(decode=True)
    assert b"collection slot" in attachments["loading-note.txt"]
    pdf = PdfReader(BytesIO(attachments["packing-list.pdf"]))
    assert "Sample A" in pdf.pages[0].extract_text() and "Total" in pdf.pages[0].extract_text()
    with BytesIO(attachments["packing-list.xlsx"]) as source:
        sheet = load_workbook(source)["Packing List"]
        assert (sheet["C5"].value, sheet["D5"].value) == (5, 100)
    with Image.open(BytesIO(attachments["packing-note.png"])) as image:
        assert image.format == "PNG"
        image.verify()

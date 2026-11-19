"""Evidence rules for sender-bound companies in current inline images."""

import re
from collections import Counter
from pathlib import Path

from .html_scope import current_inline_cids
from .local_handoff import _read_content

_COMPANY_SUFFIX = re.compile(r"\b(?:lda|ltda|ltd|limited|gmbh|llc|inc|corp|pte)\b", re.I)
_PERSONAL_DOMAINS = {"gmail", "hotmail", "outlook", "yahoo", "qq", "163", "126"}


def _box(line):
    points = line.get("location", {}).get("box", [])
    if len(points) != 4 or any(
        len(point) != 2 or not all(isinstance(n, (int, float)) for n in point) for point in points
    ):
        return None
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    return [left, top, right, bottom] if 0 <= left < right and 0 <= top < bottom else None


def _near(first, second):
    overlap = min(first[2], second[2]) - max(first[0], second[0])
    gap = max(0, max(first[1], second[1]) - min(first[3], second[3]))
    return overlap >= min(first[2] - first[0], second[2] - second[0]) / 2 and gap <= max(
        first[3] - first[1], second[3] - second[1]
    )


def _company_lines(read, domain, scope):
    if read.get("reader") != "RapidOCR" or len(read.get("parts", [])) != 1:
        return []
    lines = read["parts"][0].get("lines", [])
    width, height = scope.get("width"), scope.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return []

    def image_box(line):
        box = _box(line)
        return box if box and box[2] <= width and box[3] <= height else None

    prefix = re.compile(r"^\s*(" + re.escape(domain) + r")(?=$|[\s,;:])", re.I)
    found = []
    for index, line in enumerate(lines):
        text, box = line.get("text", ""), image_box(line)
        match = prefix.match(text) if isinstance(text, str) and box else None
        if not match:
            continue
        support = None
        if text.strip().casefold() != domain and not _COMPANY_SUFFIX.search(text):
            support = next(
                (
                    (i, other)
                    for i, other in enumerate(lines)
                    if i != index
                    and image_box(other)
                    and _near(box, image_box(other))
                    and _COMPANY_SUFFIX.search(other.get("text", ""))
                ),
                None,
            )
            if support is None:
                continue
        found.append((match.group(1), (index, line, match.start(1), match.end(1)), support))
    return found


def sender_domain(manifest, target):
    email = next(
        (item for item in manifest["emails"] if item["email_ref_id"] == target["email_ref_id"]),
        None,
    )
    sender = str((email or {}).get("from_addr") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender):
        return None
    domain = sender.rsplit("@", 1)[1].split(".", 1)[0].casefold()
    return domain if len(domain) >= 5 and domain not in _PERSONAL_DOMAINS else None


def eligible_inline_images(manifest, target, content_root):
    if (
        target.get("kind") != "body"
        or target.get("role") != "current"
        or target.get("format") != "html"
    ):
        return []
    html = _read_content(Path(content_root).resolve(), target["content"]).decode("utf-8-sig")
    cids = {value.casefold() for value in current_inline_cids(html)}
    images = [
        item
        for item in manifest["materials"]
        if item["kind"] == "inline"
        and item.get("format") in {"png", "jpeg"}
        and item.get("state") == "ready"
        and item.get("role") == "current"
        and item.get("email_ref_id") == target["email_ref_id"]
        and item.get("content")
        and item.get("cid")
    ]
    counts = Counter(
        str(item["cid"]).strip("<>").casefold()
        for item in manifest["materials"]
        if item.get("cid") and item.get("email_ref_id") == target["email_ref_id"]
    )
    return [
        item
        for item in images
        if (cid := str(item["cid"]).strip("<>").casefold()) in cids and counts[cid] == 1
    ]


def signature_section_evidence(manifest, target, image):
    return {
        "kind": "signature",
        "basis": "current_inline_cid_sender_domain_ocr",
        "body_material_id": target["material_id"],
        "body_representation_sha256": target["content"]["sha256"],
        "cid": image["cid"],
        "sender_domain": sender_domain(manifest, target),
    }

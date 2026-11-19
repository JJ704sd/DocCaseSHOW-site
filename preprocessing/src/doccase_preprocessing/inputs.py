"""Adapt supplied email evidence; never fetch URLs, reconstruct threads or infer fields."""

import base64
import binascii
import copy
import hashlib
import json
import quopri
import re
from codecs import BOM_UTF8
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import unquote

from .models import Bundle, InputError, Issue, Limits, Material

Role = Literal["current", "ancestor", "supplementary"]
_UNAVAILABLE = {"filtered", "read_failed", "unavailable"}
_EMAIL_METADATA = (
    "email_ref_id",
    "subject",
    "from_addr",
    "from_name",
    "to_addrs",
    "cc_addrs",
    "sent_at",
    "snippet",
    "thread_headers",
    "thread_summary",
    "source",
)


def _identifier(value: object, path: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{path} must be a nonempty string")
    if max_length is not None and len(value) > max_length:
        raise InputError(f"{path} exceeds {max_length} characters")
    return value


def _stable_id(task_id: str, path: str) -> str:
    identity = json.dumps([task_id, path], ensure_ascii=True, separators=(",", ":"))
    return "mat-" + hashlib.sha256(identity.encode("ascii")).hexdigest()


class _Registry:
    def __init__(self, bundle: Bundle, limits: Limits) -> None:
        self.bundle = bundle
        self.limits = limits
        self.ids: set[str] = set()

    def add(self, material: Material) -> Material:
        if material.material_id in self.ids:
            raise InputError("Duplicate material_id in request")
        if len(self.ids) >= self.limits.max_materials:
            raise InputError(f"Material count exceeds {self.limits.max_materials}")
        self.ids.add(material.material_id)
        actual = len(material.content) if material.content is not None else None
        # A PDF's declared file size says nothing about its supplied text derivative.
        declared = material.declared_size if material.representation == "original" else None
        size = max(actual or 0, declared or 0)
        if size > self.limits.max_material_bytes:
            material.issues.append(
                Issue(
                    "limit_exceeded",
                    "Material byte limit exceeded; content is not truncated",
                    {
                        "actual": actual,
                        "declared": material.declared_size,
                        "limit": self.limits.max_material_bytes,
                        "resource": "material_bytes",
                    },
                )
            )
        self.bundle.materials.append(material)
        return material


def _request_size(payload: dict, limits: Limits) -> int:
    # A dict has no original wire representation. Bound its compact UTF-8 JSON form.
    size = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for chunk in encoder.iterencode(payload):
            size += len(chunk.encode("utf-8"))
            if size > limits.max_input_bytes:
                raise InputError(f"Input byte count exceeds {limits.max_input_bytes}")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError("Request must be a UTF-8 JSON-compatible object") from error
    return size


def _email_meta(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{path} must be an EmailMeta object")
    # V1.0 sections 4.3/5.1, not the draft schema's expanded required list.
    required = (
        "email_ref_id",
        "subject",
        "from_addr",
        "from_name",
        "to_addrs",
        "sent_at",
        "snippet",
        "thread_headers",
        "body_text",
    )
    for key in required:
        if key not in value:
            raise InputError(f"{path}.{key} is required by V1.0")
    _identifier(value["email_ref_id"], f"{path}.email_ref_id", max_length=64)
    for key in ("subject", "snippet", "body_text"):
        if not isinstance(value[key], str):
            raise InputError(f"{path}.{key} must be a string")
    for key in ("from_addr", "from_name", "sent_at", "body_html"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise InputError(f"{path}.{key} must be a string or null")
    for key in ("to_addrs", "cc_addrs"):
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(item, str) for item in value[key])
        ):
            raise InputError(f"{path}.{key} must be a list of strings")
    headers = value["thread_headers"]
    if not isinstance(headers, dict):
        raise InputError(f"{path}.thread_headers must be an object")
    for key in ("message_id", "in_reply_to"):
        if headers.get(key) is not None and not isinstance(headers[key], str):
            raise InputError(f"{path}.thread_headers.{key} must be a string or null")
    if "references" in headers and (
        not isinstance(headers["references"], list)
        or any(not isinstance(item, str) for item in headers["references"])
    ):
        raise InputError(f"{path}.thread_headers.references must be a list of strings")
    if "attachments" in value and not isinstance(value["attachments"], list):
        raise InputError(f"{path}.attachments must be a list")
    return value


_CSS_URL = re.compile(
    r"""/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|"""
    r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)""",
    re.IGNORECASE | re.DOTALL,
)


class _CidReferences(HTMLParser):
    """Read resource URLs only; never rewrite HTML or load referenced resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cids: dict[str, None] = {}
        self.in_style = False
        self.in_script = False

    def _url(self, value: str) -> None:
        value = value.strip()
        if value[:4].lower() == "cid:" and value[4:]:
            self.cids.setdefault(unquote(value[4:]), None)

    def _css(self, value: str) -> None:
        # Comments and standalone string literals are consumed without becoming URLs.
        for match in _CSS_URL.finditer(value):
            for url in match.groups():
                if url is not None:
                    self._url(url)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.in_script = True
        if self.in_script:
            return
        if tag == "style":
            self.in_style = True
        for name, value in attrs:
            if value is None:
                continue
            # HTMLParser has decoded character entities in these attribute values.
            if name in {"src", "href", "background"}:
                self._url(value)
            elif name == "style":
                self._css(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False
        elif tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style and not self.in_script:
            self._css(data)


def _cid_references(text: str) -> list[str]:
    parser = _CidReferences()
    parser.feed(text)
    parser.close()
    return list(parser.cids)


def _attachment(data: object, email: dict, role: Role, path: str, reg: _Registry) -> None:
    invalid_item = not isinstance(data, dict)
    data = data if isinstance(data, dict) else {}
    supplied_id = data.get("material_id")
    material_id = (
        _identifier(supplied_id, f"{path}.material_id")
        if "material_id" in data
        else _stable_id(reg.bundle.task_id, path)
    )
    filename = data.get("filename", "attachment")
    mime = data.get("mime", "application/octet-stream")
    cid = data.get("content_id")
    disposition = data.get("disposition")
    material = Material(
        material_id,
        email["email_ref_id"],
        role,
        "inline"
        if disposition == "inline" or (cid and disposition != "attachment")
        else "attachment",
        filename if isinstance(filename, str) else "attachment",
        mime if isinstance(mime, str) else "application/octet-stream",
        metadata=copy.deepcopy(
            {
                key: data[key]
                for key in (
                    "disposition",
                    "sha256",
                    "availability",
                    "availability_reason",
                    "size_bytes",
                )
                if key in data
            }
        ),
    )
    material.metadata["source_path"] = path
    if invalid_item:
        material.issues.append(Issue("invalid_attachment", "Attachment must be an object"))
    for field, value in (("filename", filename), ("mime", mime)):
        if not isinstance(value, str):
            material.issues.append(
                Issue(
                    "invalid_attachment",
                    "Attachment label must be a string",
                    {"field": field},
                )
            )
    if isinstance(cid, str):
        material.cid = cid.strip("<> \t\r\n")
    if data.get("size_bytes") is not None:
        size = data["size_bytes"]
        if type(size) is int and size >= 0:
            material.declared_size = size
        else:
            reg.bundle.issues.append(
                Issue(
                    "invalid_size",
                    "Invalid declared size was retained as metadata",
                    {"material_id": material_id},
                )
            )
    if data.get("oss_url") is not None:
        if isinstance(data["oss_url"], str) and data["oss_url"].strip():
            material.locator = data["oss_url"]
        else:
            material.issues.append(Issue("invalid_locator", "oss_url must be a nonempty string"))
    availability = data.get("availability")
    if "availability" in data and (
        not isinstance(availability, str) or availability not in _UNAVAILABLE | {"available"}
    ):
        material.issues.append(
            Issue(
                "invalid_availability",
                "Attachment availability is not a recognized status",
            )
        )
    elif availability in _UNAVAILABLE:
        material.issues.append(
            Issue(
                data["availability"],
                "Upstream did not make this material available",
                {"reason": data.get("availability_reason")},
            )
        )
    text, encoded = data.get("content_text"), data.get("content_base64")
    if text is not None and encoded is not None:
        material.issues.append(
            Issue(
                "ambiguous_content",
                "Both content_text and content_base64 were supplied",
            )
        )
    elif text is not None:
        material.representation = "provided_text"
        if isinstance(text, str):
            material.content = text.encode("utf-8")
        else:
            material.issues.append(Issue("invalid_content_text", "content_text must be a string"))
    elif encoded is not None:
        try:
            if not isinstance(encoded, str):
                raise ValueError("Not a string")
            material.content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            material.issues.append(Issue("invalid_base64", "content_base64 is not strict base64"))
    elif material.locator is None and not material.issues:
        material.issues.append(
            Issue("missing_content", "No inline content or locator was provided")
        )
    # A locator is pending acquisition, not a blocking defect: the pipeline may resolve it.
    reg.add(material)


def _register_missing_cids(materials: list[Material], path: str, reg: _Registry) -> None:
    """Reconcile one email occurrence after all its supplied parts are registered."""
    supplied = {material.cid for material in materials if material.cid}
    references: dict[str, list[Material]] = {}
    for material in materials:
        for cid in material.metadata.get("cid_references", []):
            if cid not in supplied:
                references.setdefault(cid, []).append(material)
    for cid, referring in references.items():
        source = referring[0]
        identity = json.dumps([path, source.email_ref_id, "missing_cid", cid], ensure_ascii=True)
        reg.add(
            Material(
                _stable_id(reg.bundle.task_id, identity),
                source.email_ref_id,
                source.role,
                "inline",
                "missing-cid",
                "application/octet-stream",
                content=None,
                cid=cid,
                selected=True,
                parent_material_id=source.parent_material_id,
                issues=[
                    Issue(
                        "missing_cid",
                        "Referenced CID resource has no supplied material in this email",
                        {"cid": cid},
                    )
                ],
                metadata={
                    "source_path": path,
                    "availability": "unavailable",
                    "referenced_by_material_ids": [material.material_id for material in referring],
                },
            )
        )


def _with_utf8_bom(raw: bytes) -> bytes:
    return raw if raw.startswith(BOM_UTF8) else BOM_UTF8 + raw


def _request_email(email: dict, role: Role, path: str, reg: _Registry) -> None:
    first_material_index = len(reg.bundle.materials)
    record = copy.deepcopy({key: email[key] for key in _EMAIL_METADATA if key in email})
    record["role"] = role
    reg.bundle.emails.append(record)
    bodies = []
    group = _stable_id(reg.bundle.task_id, path + ".body")
    for key, mime, name in (
        ("body_text", "text/plain", "body.txt"),
        ("body_html", "text/html", "body.html"),
    ):
        if email.get(key) is None:
            continue
        value = email[key]
        body = Material(
            _stable_id(reg.bundle.task_id, path + "." + key),
            email["email_ref_id"],
            role,
            "body",
            name,
            mime,
            content=_with_utf8_bom(value.encode("utf-8"))
            if key == "body_html"
            else value.encode("utf-8"),
            selected=bool(value.removeprefix("\ufeff").strip()),
            alternative_group=group,
            representation="provided_text" if key == "body_text" else "derived_text",
            metadata={"source_path": path + "." + key, "charset": "utf-8"},
        )
        if key == "body_html":
            body.metadata["cid_references"] = _cid_references(value)
        bodies.append(reg.add(body))
    if any(body.mime == "text/html" and body.selected and not body.issues for body in bodies):
        for body in bodies:
            if body.mime == "text/plain":
                body.selected = False
    for index, attachment in enumerate(email.get("attachments", [])):
        _attachment(attachment, email, role, f"{path}.attachments[{index}]", reg)
    _register_missing_cids(reg.bundle.materials[first_material_index:], path, reg)


def adapt_request(payload: dict, limits: Limits | None = None) -> Bundle:
    """Accept V1.0 and optional evidence extensions, without enforcing a draft schema.

    ``context.ancestors`` / ``supplementary`` are caller-confirmed roles. Unclassified
    ``history_emails`` and draft ``context.emails`` remain supplementary occurrences.
    Input bytes and total material count reject the envelope; single-item failures
    remain in the bundle. Unused optional fields do not add validation requirements.
    """
    limits = limits if limits is not None else Limits()
    if not isinstance(payload, dict):
        raise InputError("Request must be an object")
    size = _request_size(payload, limits)
    task_id = _identifier(payload.get("task_id"), "task_id", max_length=64)
    current = _email_meta(payload.get("email"), "email")
    context = payload.get("context", {})
    if not isinstance(context, dict):
        raise InputError("context must be an object")
    bundle = Bundle(
        task_id,
        [],
        [],
        metadata=copy.deepcopy(
            {
                key: payload[key]
                for key in (
                    "schema_version",
                    "trace_id",
                    "request_id",
                    "matched_inquiry_hint",
                    "matched_candidates",
                )
                if key in payload
            }
        ),
    )
    bundle.metadata["context"] = copy.deepcopy(
        {key: context[key] for key in ("coverage", "missing_message_ids") if key in context}
    )
    bundle.metadata["input_bytes"] = size
    reg = _Registry(bundle, limits)
    _request_email(current, "current", "email", reg)
    groups = [
        (context, "ancestors", "ancestor", "context.ancestors"),
        (context, "supplementary", "supplementary", "context.supplementary"),
        (context, "emails", "supplementary", "context.emails"),
        (payload, "history_emails", "supplementary", "history_emails"),
        (current, "history_emails", "supplementary", "email.history_emails"),
    ]
    for owner, key, role, path in groups:
        emails = owner.get(key, [])
        if not isinstance(emails, list):
            raise InputError(f"{path} must be a list")
        if emails and key in {"emails", "history_emails"}:
            bundle.issues.append(
                Issue(
                    "unclassified_history",
                    "History retained as supplementary; no chain inferred",
                    {"source_path": path, "email_count": len(emails)},
                )
            )
        for index, email in enumerate(emails):
            item_path = f"{path}[{index}]"
            _request_email(_email_meta(email, item_path), role, item_path, reg)
    issues = context.get("issues", [])
    if not isinstance(issues, list):
        raise InputError("context.issues must be a list")
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("message"), str):
            raise InputError("context.issues entries require code and message strings")
        code = _identifier(issue.get("code"), "context.issues.code")
        bundle.issues.append(Issue(code, issue["message"], {"source": "context"}))
    return bundle


def _entity_body(raw: bytes) -> bytes:
    # A MIME entity may have no headers: its first empty line starts the body.
    if raw.startswith(b"\r\n"):
        return raw[2:]
    if raw.startswith((b"\n", b"\r")):
        return raw[1:]
    separator = re.search(rb"\r\n\r\n|\n\n|\r\r", raw)
    return raw[separator.end() :] if separator else b""


def _child_entities(raw: bytes, boundary: str) -> list[bytes]:
    """Slice original MIME entities; reserialization would change original bytes."""
    marker = re.escape(boundary.encode("ascii"))
    pattern = rb"(?:^|(?<=[\r\n]))--" + marker + rb"(?P<close>--)?[ \t]*(?:\r\n|\n|\r|$)"
    body = _entity_body(raw)
    parts: list[bytes] = []
    start = None
    for match in re.finditer(pattern, body):
        if start is not None:
            end = match.start()
            if body[:end].endswith(b"\r\n"):
                end -= 2
            elif body[:end].endswith((b"\n", b"\r")):
                end -= 1
            parts.append(body[start:end])
        if match.group("close"):
            start = None
            break
        start = match.end()
    if start is not None:
        parts.append(body[start:])
    return parts


def _transfer_bytes(part: Message, raw: bytes, material: Material) -> bytes:
    body = _entity_body(raw)
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    material.metadata["content_transfer_encoding"] = encoding
    try:
        if encoding == "base64":
            # MIME permits line wrapping; JSON base64 deliberately does not.
            return base64.b64decode(re.sub(rb"[ \t\r\n]", b"", body), validate=True)
        if encoding == "quoted-printable":
            if re.search(rb"=(?![0-9A-Fa-f]{2}|\r?\n)", body):
                raise ValueError("Invalid quoted-printable escape")
            return quopri.decodestring(body)
        if encoding in {"7bit", "8bit", "binary"}:
            if encoding == "7bit" and any(byte > 127 for byte in body):
                raise ValueError("Non-ASCII bytes in 7bit content")
            return body
        material.issues.append(
            Issue(
                "unsupported_transfer_encoding",
                "Content transfer encoding is not supported",
                {"encoding": encoding},
            )
        )
    except (ValueError, binascii.Error):
        material.issues.append(
            Issue(
                "invalid_transfer_encoding",
                "Invalid encoded MIME content; raw payload retained",
                {"encoding": encoding},
            )
        )
    return body


def _decode_text(part: Message, material: Material) -> None:
    if material.issues or material.content is None:
        return
    charset = part.get_content_charset() or "us-ascii"  # RFC default, not a guessed codec.
    material.metadata["source_charset"] = charset
    try:
        text = material.content.decode(charset, errors="strict")
        normalized = text.encode("utf-8", errors="strict")
    except LookupError:
        material.issues.append(
            Issue(
                "unknown_charset",
                "Declared text charset is not supported",
                {"charset": charset},
            )
        )
        return
    except (UnicodeError, ValueError):
        material.issues.append(
            Issue(
                "invalid_text_encoding",
                "Text does not decode with its declared charset",
                {"charset": charset},
            )
        )
        return
    material.content = normalized
    material.representation = "derived_text"
    material.metadata["charset"] = "utf-8"
    if material.mime == "text/html":
        material.content = _with_utf8_bom(normalized)
        material.metadata["cid_references"] = _cid_references(text)
    if material.kind == "body" and not text.removeprefix("\ufeff").strip():
        material.selected = False


def _header_text(raw: str, bundle: Bundle, name: str) -> str:
    try:
        chunks = decode_header(raw)
        result = "".join(
            value.decode(charset or "ascii", errors="strict") if isinstance(value, bytes) else value
            for value, charset in chunks
        )
        result.encode("utf-8", errors="strict")
        return result
    except (LookupError, UnicodeError, ValueError):
        bundle.issues.append(
            Issue(
                "invalid_header_encoding",
                "Header could not be strictly decoded; raw EML retained",
                {"header": name},
            )
        )
        return raw.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _eml_metadata(message: Message, bundle: Bundle, email_ref_id: str) -> None:
    headers: dict[str, str] = {}
    address_headers: dict[str, list[str]] = {"to": [], "cc": []}
    raw_headers = []
    for name, value in message.raw_items():
        raw_headers.append([name, value.encode("utf-8", "backslashreplace").decode("utf-8")])
        decoded = _header_text(value, bundle, name)
        headers.setdefault(name.lower(), decoded)
        if name.lower() in address_headers:
            address_headers[name.lower()].append(decoded)
    from_name, from_addr = parseaddr(headers.get("from", ""))
    references = headers.get("references", "")
    bundle.emails.append(
        {
            "email_ref_id": email_ref_id,
            "role": "current",
            "subject": headers.get("subject"),
            "from_addr": from_addr,
            "from_name": from_name,
            "to_addrs": [addr for _, addr in getaddresses(address_headers["to"]) if addr],
            "cc_addrs": [addr for _, addr in getaddresses(address_headers["cc"]) if addr],
            "sent_at": headers.get("date"),
            "raw_headers": raw_headers,
            "thread_headers": {
                "message_id": headers.get("message-id"),
                "in_reply_to": headers.get("in-reply-to"),
                "references": re.findall(r"<[^<>]+>", references),
                "raw_references": references,
            },
            "source": {"artifact_kind": "original_eml"},
        }
    )


def _eml_part(
    part: Message,
    raw: bytes,
    path: str,
    parent: Material,
    reg: _Registry,
    *,
    body_allowed: bool = True,
) -> list[Material]:
    mime = part.get_content_type()
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    cid = str(part.get("Content-ID", "")).strip("<> \t\r\n") or None
    for defect in part.defects:
        reg.bundle.issues.append(
            Issue(
                "mime_defect",
                "MIME parser reported a structural defect; original EML retained",
                {"mime_path": path, "defect": type(defect).__name__},
            )
        )
    is_container = mime.startswith("multipart/") and not filename and disposition != "attachment"
    if is_container and part.is_multipart():
        children = part.get_payload()
        try:
            entities = _child_entities(raw, part.get_boundary() or "")
        except UnicodeError:
            entities = []
        if len(entities) == len(children) and entities:
            branches = []
            related_start = (part.get_param("start") or "").strip("<>")
            for index, (child, entity) in enumerate(zip(children, entities, strict=True)):
                is_root = (
                    str(child.get("Content-ID", "")).strip("<>") == related_start
                    if related_start
                    else index == 0
                )
                branches.append(
                    _eml_part(
                        child,
                        entity,
                        f"{path}.{index}",
                        parent,
                        reg,
                        body_allowed=body_allowed and (mime != "multipart/related" or is_root),
                    )
                )
            if mime == "multipart/alternative":
                group = _stable_id(reg.bundle.task_id, path + ".alternative")
                html = [
                    i
                    for i, branch in enumerate(branches)
                    if any(
                        m.kind == "body"
                        and m.mime == "text/html"
                        and m.selected
                        and not m.issues
                        and m.content
                        and m.content.strip()
                        for m in branch
                    )
                ]
                nonempty = [
                    i
                    for i, branch in enumerate(branches)
                    if any(
                        m.kind == "body"
                        and m.selected
                        and not m.issues
                        and m.content
                        and m.content.strip()
                        for m in branch
                    )
                ]
                chosen = (html or nonempty or [0])[-1]
                for index, branch in enumerate(branches):
                    for material in branch:
                        if material.kind == "body":
                            material.alternative_group = group
                            if index != chosen:
                                material.selected = False
            return [material for branch in branches for material in branch]
    if mime == "message/rfc822":
        kind = "attachment"
    elif filename or disposition == "attachment":
        kind = "inline" if disposition == "inline" else "attachment"
    elif mime in {"text/plain", "text/html"} and body_allowed:
        kind = "body"
    else:
        kind = "inline" if cid or disposition == "inline" else "attachment"
    metadata = {"mime_path": path, "disposition": disposition}
    if kind == "inline" and filename:
        metadata["mime_filename"] = filename
    material = Material(
        _stable_id(reg.bundle.task_id, path),
        parent.email_ref_id,
        "current",
        kind,
        filename or ("body.html" if mime == "text/html" else "body.txt")
        if kind == "body"
        else filename or ("attached.eml" if mime == "message/rfc822" else "mime-part"),
        mime,
        cid=cid,
        parent_material_id=parent.material_id,
        metadata=metadata,
    )
    material.content = _transfer_bytes(part, raw, material)
    if mime == "message/rfc822" or mime.startswith("multipart/"):
        material.issues.append(
            Issue(
                "unsupported",
                "Nested email or opaque MIME container retained without expansion",
            )
        )
    elif mime.startswith("text/"):
        _decode_text(part, material)
    return [reg.add(material)]


def adapt_eml(
    raw: bytes, *, task_id: str, email_ref_id: str, limits: Limits | None = None
) -> Bundle:
    """Unpack MIME with stdlib, retaining exact EML bytes and occurrence provenance.

    Text uses only its declared charset (RFC default US-ASCII when absent), strictly.
    UTF-8 body derivatives point to the original EML; invalid payloads remain blocked.
    Nested message/rfc822 payloads are opaque attachments, never current-email bodies.
    """
    limits = limits if limits is not None else Limits()
    task_id = _identifier(task_id, "task_id", max_length=64)
    email_ref_id = _identifier(email_ref_id, "email_ref_id", max_length=64)
    if not isinstance(raw, bytes) or not raw:
        raise InputError("EML must be nonempty bytes")
    if len(raw) > limits.max_input_bytes:
        raise InputError(f"Input byte count exceeds {limits.max_input_bytes}")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if not message.keys():
            raise InputError("Input does not contain a recognizable email header")
        bundle = Bundle(task_id, [], [], metadata={"input_bytes": len(raw), "input_format": "eml"})
        reg = _Registry(bundle, limits)
        original = reg.add(
            Material(
                _stable_id(task_id, email_ref_id + ".original_email"),
                email_ref_id,
                "current",
                "original_email",
                "original.eml",
                "message/rfc822",
                content=raw,
                selected=False,
            )
        )
        _eml_metadata(message, bundle, email_ref_id)
        materials = _eml_part(message, raw, email_ref_id + ".mime", original, reg)
        _register_missing_cids(materials, email_ref_id + ".mime", reg)
        return bundle
    except RecursionError as error:
        raise InputError("EML structure is too deeply nested to recognize safely") from error

"""Resolve local email and MIME occurrences from an existing attachment inventory."""

import hashlib
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from doccase_preprocessing.models import Limits


class IndexedSourceError(ValueError):
    """A selected indexed occurrence cannot be verified against its local archive."""


def _identity(kind, parts):
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return kind + "-" + hashlib.sha256(raw).hexdigest()[:40]


class LocalEmailIndex:
    """A read-only snapshot of indexed MIME occurrences, not a complete archive listing."""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        inventory = json.loads(raw)
        archives = {}
        for archive in inventory["archives"]:
            archive_id = archive["id"]
            name = Path(archive["path"]).name
            if not archive_id or not name.lower().endswith(".zip") or archive_id in archives:
                raise ValueError("Invalid archive identity in local index")
            archives[archive_id] = name
        self.emails = {}
        for item in inventory["items"]:
            digest = item["sha256"]
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("Invalid attachment SHA-256 in local index")
            for ref in item.get("mime_refs", []):
                archive_id, member, part_index = ref["archive"], ref["eml"], ref["part_index"]
                if (
                    archive_id not in archives
                    or not isinstance(member, str)
                    or not member
                    or type(part_index) is not int
                    or part_index < 0
                ):
                    raise ValueError("Invalid email occurrence in local index")
                email_id = _identity("email", [archive_id, member])
                email = self.emails.setdefault(
                    email_id,
                    {
                        "email_id": email_id,
                        "archive_id": archive_id,
                        "archive": archives[archive_id],
                        "member": member,
                        "attachments": [],
                    },
                )
                if (email["archive_id"], email["member"]) != (archive_id, member):
                    raise ValueError("Email identity collision in local index")
                attachment_id = _identity("attachment", [archive_id, member, part_index, digest])
                extension = item["extension"].lower()
                disposition = ref.get("disposition")
                filename = ref.get("filename")
                indexed_content_id = ref.get("content_id")
                if indexed_content_id is not None and not isinstance(indexed_content_id, str):
                    raise ValueError("Invalid content ID in local index")
                supported = (
                    extension == ".pdf" and isinstance(filename, str) and bool(filename.strip())
                )
                attachment = {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "extension": extension,
                    "disposition": disposition,
                    "content_id": indexed_content_id,
                    "content_id_indexed": "content_id" in ref,
                    "part_index": part_index,
                    "sha256": digest,
                    "size_bytes": item["size"],
                    "supported": supported,
                    "unsupported_reason": None
                    if supported
                    else "missing_filename_or_non_pdf_not_supported",
                }
                if any(old["part_index"] == part_index for old in email["attachments"]):
                    raise ValueError("Duplicate MIME occurrence in local index")
                email["attachments"].append(attachment)
        self.ordered = sorted(
            self.emails.values(), key=lambda x: (x["archive_id"], x["member"].casefold())
        )

    def search(self, query, offset, limit):
        query = query.casefold()
        matched = [
            email
            for email in self.ordered
            if not query
            or query in email["member"].casefold()
            or query in email["archive_id"].casefold()
            or query in email["email_id"].casefold()
            or any(query in (item["filename"] or "").casefold() for item in email["attachments"])
        ]
        return {
            "index_sha256": self.sha256,
            "coverage": "indexed_mime_occurrences_only",
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "emails": matched[offset : offset + limit],
        }

    def select(self, email_id, attachment_id):
        email = self.emails.get(email_id)
        if email is None:
            raise ValueError("Email is not present in the local index")
        matches = [x for x in email["attachments"] if x["attachment_id"] == attachment_id]
        if len(matches) != 1:
            raise ValueError("Attachment is not indexed for the selected email")
        if not matches[0]["supported"]:
            raise ValueError(
                "Only named PDF attachments or inline files are supported by this entry"
            )
        return email, matches[0]


def _leaf_paths(part, path):
    disposition = part.get_content_disposition()
    is_container = (
        part.get_content_type().startswith("multipart/")
        and not part.get_filename()
        and disposition != "attachment"
        and part.is_multipart()
    )
    if not is_container:
        return {id(part): path}
    leaves = {}
    for index, child in enumerate(part.get_payload()):
        leaves.update(_leaf_paths(child, f"{path}.{index}"))
    return leaves


def read_indexed_email(index, archive_root, email_id, attachment_id=None):
    """Read an indexed email, verifying the MIME occurrence when one is selected."""
    if attachment_id is None:
        email = index.emails.get(email_id)
        if email is None:
            raise ValueError("Email is not present in the local index")
        attachment = None
    else:
        email, attachment = index.select(email_id, attachment_id)
    try:
        root = Path(archive_root).resolve(strict=True)
        archive_path = (root / email["archive"]).resolve(strict=True)
        if not archive_path.is_relative_to(root):
            raise IndexedSourceError("Indexed archive is outside the archive root")
        with ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if item.filename == email["member"]]
            if len(members) != 1:
                raise IndexedSourceError("Indexed email member is missing or duplicated")
            info = members[0]
            limit = Limits().max_input_bytes
            if info.file_size > limit:
                raise IndexedSourceError("Indexed email exceeds the input limit")
            with archive.open(info) as stream:
                raw = stream.read(limit + 1)
            if len(raw) != info.file_size:
                raise IndexedSourceError("Indexed email could not be read completely")
        if attachment is None:
            return raw, {
                "index_sha256": index.sha256,
                "email_id": email_id,
                "archive_id": email["archive_id"],
                "archive": email["archive"],
                "email_member": email["member"],
                "email_sha256": hashlib.sha256(raw).hexdigest(),
            }
        message = BytesParser(policy=policy.default).parsebytes(raw)
        parts = list(message.walk())
        part_index = attachment["part_index"]
        if part_index >= len(parts):
            raise IndexedSourceError("Indexed MIME part is missing")
        part = parts[part_index]
        paths = _leaf_paths(message, email_id + ".mime")
        mime_path = paths.get(id(part))
        if mime_path is None:
            raise IndexedSourceError("Nested email MIME parts are not supported")
        payload = part.get_payload(decode=True)
        content_id = str(part.get("Content-ID", "")).strip() or None
        indexed_content_id = (attachment["content_id"] or "").strip() or None
        if (
            not isinstance(payload, bytes)
            or len(payload) != attachment["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != attachment["sha256"]
            or part.get_filename() != attachment["filename"]
            or part.get_content_disposition() != attachment["disposition"]
            or (attachment["content_id_indexed"] and indexed_content_id != content_id)
        ):
            raise IndexedSourceError("Indexed MIME attachment differs from the local email")
    except (OSError, BadZipFile) as error:
        raise IndexedSourceError("Indexed local archive is unavailable or unreadable") from error
    source = {
        "index_sha256": index.sha256,
        "archive_id": email["archive_id"],
        "archive": email["archive"],
        "email_member": email["member"],
        "email_sha256": hashlib.sha256(raw).hexdigest(),
        "attachment_id": attachment_id,
        "attachment_sha256": attachment["sha256"],
        "mime_part_index": part_index,
        "mime_path": mime_path,
        "mime_filename": part.get_filename(),
        "mime_disposition": part.get_content_disposition(),
        "mime_content_id": content_id,
    }
    return raw, source

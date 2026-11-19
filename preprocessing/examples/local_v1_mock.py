"""Synthetic localhost-only query/callback receiver for the saved draft preview."""

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from doccase_preprocessing.business_result import read_projection_assessment
from doccase_preprocessing.output_project import Unrepresentable, read_current_draft_preview
from doccase_preprocessing.output_snapshot import SnapshotValidationError


def exercise(project_root, assessment_id, receiver_output):
    assessment = read_projection_assessment(Path(project_root) / "assessments", assessment_id)
    if assessment["state"] != "draft_preview" or assessment["delivery_state"] != "not_queued":
        raise ValueError("Only a saved draft preview may enter the synthetic local exercise")
    projection_id = assessment["payload_ref"]["projection_id"]
    preview = read_current_draft_preview(project_root, projection_id)
    initial = preview["wire_payload"]
    body = preview["wire_payload_json"].encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    if body_hash != preview["wire_sha256"]:
        raise ValueError("Stored draft wire payload hash mismatch")
    secret = b"synthetic-local-test-only"
    seen_nonces, accepted = set(), {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self, raw):
            timestamp, nonce = (
                self.headers.get("X-AI-Timestamp", ""),
                self.headers.get("X-AI-Nonce", ""),
            )
            signature = self.headers.get("X-AI-Signature", "")
            try:
                fresh = abs(int(time.time() * 1000) - int(timestamp)) <= 300000
            except ValueError:
                fresh = False
            signed = (timestamp + "\n" + nonce + "\n" + hashlib.sha256(raw).hexdigest()).encode(
                "utf-8"
            )
            expected = "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
            if (
                not fresh
                or not nonce
                or nonce in seen_nonces
                or not hmac.compare_digest(signature, expected)
            ):
                return False
            seen_nonces.add(nonce)
            return True

        def do_GET(self):
            if not self.authorized(b""):
                return self.respond(401, {"code": "AI-AUTH-001"})
            try:
                current = read_current_draft_preview(project_root, projection_id)["wire_payload"]
            except Unrepresentable as error:
                return self.respond(409, {"code": error.code, "detail": error.detail})
            except SnapshotValidationError:
                return self.respond(409, {"code": "local-draft-invalid"})
            if self.path != "/ai/v1/tasks/" + quote(current["task_id"], safe=""):
                return self.respond(404, {"code": "AI-TASK-001"})
            result = {
                key: value for key, value in current.items() if key not in {"task_id", "result"}
            }
            self.respond(
                200,
                {
                    "code": "0",
                    "message": "success",
                    "data": {
                        "task_id": current["task_id"],
                        "biz_type": "parse",
                        "state": "succeeded",
                        "result": result,
                        "error": None,
                    },
                },
            )

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/ai/callback/parse" or not self.authorized(raw):
                return self.respond(401, {"code": "AI-AUTH-001"})
            try:
                current = read_current_draft_preview(project_root, projection_id)["wire_payload"]
            except Unrepresentable as error:
                return self.respond(409, {"code": error.code, "detail": error.detail})
            except SnapshotValidationError:
                return self.respond(409, {"code": "local-draft-invalid"})
            incoming = json.loads(raw)
            if incoming != current:
                return self.respond(409, {"code": "local-payload-mismatch"})
            task_id = incoming["task_id"]
            duplicate = task_id in accepted
            if not duplicate:
                accepted[task_id] = hashlib.sha256(raw).hexdigest()
                with Path(receiver_output).open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(incoming, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            self.respond(
                200,
                {
                    "code": "0",
                    "message": "received",
                    "data": {"task_id": task_id, "deduplicated": duplicate},
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_address[1])
    try:

        def headers_for(raw):
            timestamp, nonce = str(int(time.time() * 1000)), uuid4().hex
            digest = hashlib.sha256(raw).hexdigest()
            message = (timestamp + "\n" + nonce + "\n" + digest).encode("utf-8")
            signature = "sha256=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
            return {
                "Content-Type": "application/json",
                "X-AI-Timestamp": timestamp,
                "X-AI-Nonce": nonce,
                "X-AI-Signature": signature,
            }

        query_request = Request(
            base + "/ai/v1/tasks/" + quote(initial["task_id"], safe=""), headers=headers_for(b"")
        )
        with urlopen(query_request, timeout=5) as response:
            query = json.load(response)
        acknowledgements = []
        replay_rejected = False
        for _ in range(2):
            request = Request(
                base + "/ai/callback/parse", data=body, headers=headers_for(body), method="POST"
            )
            with urlopen(request, timeout=5) as response:
                acknowledgements.append(json.load(response))
            if len(acknowledgements) == 1:
                try:
                    urlopen(request, timeout=5)
                except HTTPError as error:
                    replay_rejected = error.code == 401
        received = json.loads(Path(receiver_output).read_text(encoding="utf-8"))
        projected = {
            key: value for key, value in received.items() if key not in {"task_id", "result"}
        }
        if (
            received != initial
            or query["data"]["result"] != projected
            or not replay_rejected
            or [ack["data"]["deduplicated"] for ack in acknowledgements] != [False, True]
        ):
            raise AssertionError(
                "Local query, callback or idempotence did not match the saved draft"
            )
        return {
            "kind": "synthetic_loopback_only",
            "saved_payload_sha256": body_hash,
            "query": query,
            "callback_acknowledgements": acknowledgements,
            "replayed_nonce_rejected": replay_rejected,
            "receiver_ref": str(receiver_output),
            "query_matches_callback": True,
            "external_notification_sent": False,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

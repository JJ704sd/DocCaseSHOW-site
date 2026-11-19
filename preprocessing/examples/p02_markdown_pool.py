"""Bounded, offline Markdown export; a failed worker never publishes its output."""

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4


def _failure(reason):
    return {"state": "failure", "reason": reason}


class _Slot:
    def __init__(self):
        self.process = None

    def stop(self):
        proc, self.process = self.process, None
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdin.close()
            proc.stdout.close()

    def exchange(self, envelope, deadline):
        replies = Queue(1)
        proc = self.process

        def io():
            try:
                proc.stdin.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                proc.stdin.flush()
                replies.put(proc.stdout.readline())
            except (OSError, ValueError):
                replies.put("")

        thread = Thread(target=io, daemon=True)
        thread.start()
        try:
            raw = replies.get(timeout=max(0, deadline - monotonic()))
        except Empty:
            self.stop()
            thread.join()
            return _failure("timeout")
        try:
            reply = json.loads(raw)
            if reply["id"] != envelope["id"] or not isinstance(reply["result"], dict):
                raise ValueError("Invalid reply")
            return reply["result"]
        except (ValueError, TypeError, KeyError):
            self.stop()
            return _failure("worker_exit" if not raw else "invalid_worker_reply")


class MarkdownWorkerPool:
    """Accept only export requests. Capacity and timeout include slot wait."""

    def __init__(self, command, environment, slots):
        if type(slots) is not int or not 1 <= slots <= 16:
            raise ValueError("Markdown worker slots must be between 1 and 16")
        self._command, self._environment = command, environment
        self._slots = Queue(slots)
        for _ in range(slots):
            self._slots.put(_Slot())
        self._size = slots
        self._closed = False
        self._counts = Counter()
        self._lock = Lock()

    @property
    def stats(self):
        with self._lock:
            return dict(self._counts)

    def _record(self, key):
        with self._lock:
            self._counts[key] += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        # Callers end their submissions before closing the service.
        if not self._closed:
            self._closed = True
            for _ in range(self._size):
                self._slots.get().stop()

    def __call__(self, request):
        if request.get("operation") != "export_markdown" or self._closed:
            raise ValueError("Expected an open Markdown pool and an export_markdown request")
        timeout = request.get("hard_timeout_seconds", 45)
        deadline = monotonic() + timeout
        try:
            slot = self._slots.get(timeout=max(0, timeout))
        except Empty:
            self._record("queue_timeout")
            return _failure("queue_timeout")
        try:
            result = self._export(slot, request, deadline)
        finally:
            self._slots.put(slot)
        self._record(result.get("reason", result["state"]))
        return result

    def _export(self, slot, request, deadline):
        target = Path(request["output_dir"]).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return _failure("export_destination_exists")
        with TemporaryDirectory(prefix=".markdown-", dir=target.parent) as temp:
            staged = Path(temp) / "result"
            if slot.process is None or slot.process.poll() is not None:
                slot.stop()
                try:
                    slot.process = subprocess.Popen(
                        self._command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        env=self._environment,
                    )
                except OSError:
                    return _failure("worker_start_failed")
                self._record("process_starts")
            else:
                self._record("process_reuses")
            result = slot.exchange(
                {"id": uuid4().hex, "request": {**request, "output_dir": str(staged)}}, deadline
            )
            if result.get("state") == "failure":
                return result
            try:
                if (
                    result["state"] not in {"success", "partial_success"}
                    or result["read_sha256"] != request["read_sha256"]
                ):
                    raise ValueError("Unexpected export result")
                for kind, name in (("markdown", "content.md"), ("index", "index.json")):
                    file = staged / name
                    if (
                        Path(result[kind + "_ref"]).resolve() != file.resolve()
                        or file.is_symlink()
                        or hashlib.sha256(file.read_bytes()).hexdigest() != result[kind + "_sha256"]
                    ):
                        raise ValueError("Invalid staged output")
                    result[kind + "_ref"] = str(target / name)
                if monotonic() > deadline:
                    return _failure("timeout")
                staged.rename(target)
            except (OSError, TypeError, ValueError, KeyError):
                slot.stop()
                return _failure("invalid_export_artifact")
            return result

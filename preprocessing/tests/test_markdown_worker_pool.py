import sys
from time import monotonic

import pytest

from examples import p02_local_read


@pytest.mark.parametrize("failure", ["timeout", "worker_exit"])
def test_failed_markdown_export_is_not_published_and_next_job_recovers(tmp_path, failure):
    factory = getattr(p02_local_read, "markdown_worker_pool", None)
    assert callable(factory), "The READ adapter must expose a recoverable Markdown worker pool"
    script = tmp_path / "worker.py"
    script.write_text(
        """
import hashlib, json, os, sys, time
from pathlib import Path
for line in sys.stdin:
    envelope = json.loads(line)
    request = envelope['request']
    dest = Path(request['output_dir'])
    dest.mkdir()
    (dest / 'content.md').write_bytes(b'table')
    if request['mode'] == 'timeout':
        time.sleep(20)
    if request['mode'] == 'worker_exit':
        os._exit(17)
    (dest / 'index.json').write_bytes(b'{}')
    result = {'state': 'success', 'read_sha256': request['read_sha256']}
    for field, name in [('markdown','content.md'),('index','index.json')]:
        result[field + '_ref'] = str(dest / name)
        result[field + '_sha256'] = hashlib.sha256((dest / name).read_bytes()).hexdigest()
    print(json.dumps({'id': envelope['id'], 'result': result}), flush=True)
""",
        encoding="utf-8",
    )
    dest = tmp_path / "failed"
    req = {"operation": "export_markdown", "read_sha256": "test-read"}
    with factory(slots=1, python=sys.executable, script=script) as pool:
        start = monotonic()
        result = pool(
            {**req, "output_dir": str(dest), "mode": failure, "hard_timeout_seconds": 0.8}
        )
        assert result == {"state": "failure", "reason": failure}
        assert monotonic() - start < 3
        assert not dest.exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["worker.py"]
        next_dest = tmp_path / "next"
        result = pool(
            {**req, "output_dir": str(next_dest), "mode": "ok", "hard_timeout_seconds": 3}
        )
        assert result["state"] == "success"
        assert result["markdown_ref"] == str(next_dest / "content.md")
        assert (next_dest / "index.json").read_bytes() == b"{}"
        assert pool.stats["process_starts"] == 2
        assert pool.stats[failure] == 1

"""Durable JSON replacement with bounded Windows reader-sharing retries."""
import json
import os
import time
import uuid
from pathlib import Path


def write_json(path, value, *, fsync=False, newline=None, _direct=False):
    path = Path(path)
    if not _direct:
        from frame_store import active, report_write, persist_references
        if active():
            if path.name in ('run.json', 'inspection.json'):
                return report_write(path, value)
            persist_references(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8', newline=newline) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    deadline = time.monotonic() + 2
    delay = .025
    while True:
        try:
            temporary.replace(path)
            return
        except PermissionError as error:
            # Readers/antivirus may temporarily omit FILE_SHARE_DELETE. Retry
            # only the already-written file replacement, never the game action.
            if getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(.2, delay * 2)

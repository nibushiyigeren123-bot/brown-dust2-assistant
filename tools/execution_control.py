"""Persist global control signals even when a module catches its exception."""
import json
import os
from pathlib import Path
from atomic_json import write_json


class ExecutionControlError(RuntimeError):
    def __init__(self, reason, kind='control'):
        super().__init__(reason)
        self.kind = kind


def stop_execution(reason, kind='control'):
    path = os.environ.get('BD2_CONTROL_FILE')
    if path:
        write_json(Path(path), {'kind': kind, 'reason': str(reason)})
    raise ExecutionControlError(reason, kind)


def read_control(path):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None

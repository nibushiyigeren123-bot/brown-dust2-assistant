"""Network patience is independent of resource budgets and game journal identity."""
import json
import time
from project_paths import ROOT


def network_wait_seconds(root=ROOT):
    path = root / 'config/runtime_preferences.json'
    value = json.loads(path.read_text(encoding='utf-8')).get('network_wait_seconds', 120) if path.exists() else 120
    if type(value) not in (int, float) or not 30 <= value <= 600:
        raise ValueError('网络等待时限应为30～600秒')
    return float(value)


def wait_observed(observe, matches, timeout, on_wait=None, stop=None, clock=time.monotonic, sleep=time.sleep):
    """Only observes and waits. It has no input, retry-submit or journal-clearing API."""
    start = clock()
    while True:
        if stop:
            stop()
        value = observe()
        if matches(value):
            return value
        elapsed = clock() - start
        if elapsed >= timeout:
            raise RuntimeError(f'等待页面响应已达{timeout:g}秒；已停止且保留未决记录，不自动重发。')
        if on_wait:
            on_wait(elapsed, timeout)
        sleep(min(1.5, timeout - elapsed))
